"""HoldingsScraper — orchestrates Holdings_Providers with per-provider refresh
cadence.

Refresh modes (per Requirement 6):
  - "daily": fetch fresh on the first cycle of each US-Eastern trading day.
  - "filing_detect": poll the SEC EDGAR submissions index daily; only re-fetch
    the full information-table XML when the accession number changes.

State lives on disk at {disk_cache_dir}/holdings/{provider_name}.json so the
bot survives restarts without burning HTTP requests.

If all providers fail in a refresh cycle, the scraper falls back to the last
valid cached snapshot (Requirement 6.6).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from smart_trader.core.smart_money import (
    DataProvider,
    FundHoldings,
    _init_providers_from_specs,
)
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


class HoldingsScraper:
    def __init__(self, config: SmartMoneyConfig):
        self.config = config
        self._providers: List[DataProvider] = []
        # provider_name -> {"last_refresh_date": "YYYY-MM-DD",
        #                   "last_accession": "...",
        #                   "holdings": [FundHoldings dicts]}
        self._state: Dict[str, Dict] = {}
        self._init_providers()
        self._restore_disk_cache()

    @property
    def n_enabled_providers(self) -> int:
        return len(self._providers)

    # ---------------------------------------------------------------- init
    def _init_providers(self) -> None:
        """Instantiate only enabled, importable holdings providers.

        Each provider is imported lazily and only when its config toggle is on;
        a disabled or not-shipped provider module is skipped with a warning
        rather than crashing the whole pipeline at import time.
        """
        # (enabled, module_name, class_name)
        specs = [
            (self.config.berkshire_enabled, "berkshire_13f", "BerkshireProvider"),
            (self.config.ark_enabled, "ark_invest", "ARKProvider"),
            (self.config.pershing_square_enabled, "pershing_square_13f", "PershingSquareProvider"),
            (self.config.appaloosa_enabled, "appaloosa_13f", "AppaloosaProvider"),
            (self.config.duquesne_enabled, "duquesne_13f", "DuquesneProvider"),
            # Tier-A 13F additions (2026-04)
            (self.config.tci_enabled, "tci_13f", "TCIProvider"),
            (self.config.baupost_enabled, "baupost_13f", "BaupostProvider"),
            (self.config.akre_enabled, "akre_capital_13f", "AkreCapitalProvider"),
            (self.config.viking_enabled, "viking_global_13f", "VikingGlobalProvider"),
            (self.config.altimeter_enabled, "altimeter_13f", "AltimeterProvider"),
            (self.config.third_point_enabled, "third_point_13f", "ThirdPointProvider"),
            (self.config.lone_pine_enabled, "lone_pine_13f", "LonePineProvider"),
            (self.config.greenlight_enabled, "greenlight_13f", "GreenlightProvider"),
            # Tier-B ETF additions (2026-04)
            (self.config.moat_enabled, "moat_etf", "MOATProvider"),
            (self.config.cgdv_enabled, "cgdv_etf", "CGDVProvider"),
            (self.config.syld_enabled, "syld_etf", "SYLDProvider"),
        ]
        _init_providers_from_specs(self._providers, specs, self.config)

        logger.info(
            f"HoldingsScraper: initialized {len(self._providers)} providers: "
            f"{[p.provider_name for p in self._providers]}"
        )

    def _cache_dir(self) -> Path:
        return Path(self.config.disk_cache_dir) / "holdings"

    # ---------------------------------------------------------------- main
    def fetch_all(self) -> List[FundHoldings]:
        """Return the consolidated FundHoldings list across all providers.

        Applies per-provider refresh mode; falls back to cached snapshots when
        no refresh is due (or when a fetch fails).
        """
        today_et = datetime.now(_ET).date().isoformat()
        combined: List[FundHoldings] = []
        any_success = False

        for provider in self._providers:
            name = provider.provider_name
            try:
                needed = self._needs_refresh(provider, today_et)
            except Exception as e:
                logger.warning(f"  {name}: refresh check failed: {e}")
                needed = False

            if needed:
                logger.info(f"  {name}: refresh due (mode={provider.refresh_mode()})")
                try:
                    fresh = provider.fetch_holdings()
                except Exception as e:
                    logger.error(f"  {name}: fetch_holdings failed: {e}")
                    fresh = []
                if fresh:
                    self._state[name] = {
                        "last_refresh_date": today_et,
                        "last_accession": self._lookup_accession(provider),
                        "holdings": [_holding_to_dict(h) for h in fresh],
                    }
                    self._write_disk_cache(name)
                    combined.extend(fresh)
                    any_success = True
                else:
                    # Fall back to cached data if fresh fetch returned empty
                    cached = self._cached_holdings(name)
                    if cached:
                        logger.info(f"  {name}: fetch empty, using stale cache ({len(cached)} holdings)")
                        combined.extend(cached)
            else:
                cached = self._cached_holdings(name)
                if cached:
                    logger.debug(f"  {name}: cache hit ({len(cached)} holdings)")
                    combined.extend(cached)
                    any_success = True

        if not any_success and not combined:
            logger.error("HoldingsScraper: no holdings from any provider (fresh or cached)")

        logger.info(f"HoldingsScraper: total holdings = {len(combined)}")
        return combined

    # ---------------------------------------------------------------- refresh decision
    def _needs_refresh(self, provider: DataProvider, today_et: str) -> bool:
        name = provider.provider_name
        state = self._state.get(name, {})
        mode = provider.refresh_mode()

        if mode == "daily":
            return state.get("last_refresh_date") != today_et

        if mode == "filing_detect":
            # Poll at most once per day. If no cache at all, refresh.
            if state.get("last_refresh_date") != today_et:
                # Check submissions index for a new accession number
                current_acc = self._lookup_accession(provider)
                last_acc = state.get("last_accession")
                if current_acc is None:
                    # Can't determine — if we have nothing cached, try fetching anyway
                    return not state.get("holdings")
                if current_acc != last_acc:
                    logger.info(f"  {name}: new 13F accession detected ({last_acc} -> {current_acc})")
                    return True
                # Same accession, just record the poll
                state["last_refresh_date"] = today_et
                self._state[name] = state
                self._write_disk_cache(name)
            return False

        # Unknown mode — be conservative
        return state.get("last_refresh_date") != today_et

    @staticmethod
    def _lookup_accession(provider: DataProvider) -> Optional[str]:
        """Return latest accession for filing_detect providers, else None."""
        if provider.refresh_mode() != "filing_detect":
            return None
        # The 13F providers wrap a shared utility — look up the latest filing.
        try:
            from smart_trader.core.smart_money_providers._edgar_13f import get_latest_13f_filing
            # Use the provider's module to extract CIK
            cik = _extract_provider_cik(provider)
            if not cik:
                return None
            result = get_latest_13f_filing(cik)
            if result is None:
                return None
            accession, _ = result
            return accession
        except Exception:
            return None

    # ---------------------------------------------------------------- cache I/O
    def _cached_holdings(self, name: str) -> List[FundHoldings]:
        entries = self._state.get(name, {}).get("holdings") or []
        return [_dict_to_holding(d) for d in entries]

    def _restore_disk_cache(self) -> None:
        cache_dir = self._cache_dir()
        if not cache_dir.exists():
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"HoldingsScraper: cannot create {cache_dir}: {e}")
            return
        for f in cache_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                name = data.get("provider_name") or f.stem
                self._state[name] = {
                    "last_refresh_date": data.get("last_refresh_date"),
                    "last_accession": data.get("last_accession"),
                    "holdings": data.get("holdings") or [],
                }
                logger.info(
                    f"  restored holdings cache {f.name}: "
                    f"{len(self._state[name]['holdings'])} rows "
                    f"(last_refresh={self._state[name]['last_refresh_date']})"
                )
            except Exception as e:
                logger.warning(f"  holdings cache {f.name}: corrupt, discarding ({e})")

    def _write_disk_cache(self, name: str) -> None:
        state = self._state.get(name)
        if not state:
            return
        cache_dir = self._cache_dir()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            out = {
                "provider_name": name,
                "last_refresh_date": state.get("last_refresh_date"),
                "last_accession": state.get("last_accession"),
                "holdings": state.get("holdings") or [],
            }
            (cache_dir / f"{name}.json").write_text(json.dumps(out, indent=2))
        except Exception as e:
            logger.warning(f"  {name}: failed to write holdings cache: {e}")


# ---------------------------------------------------------------------------

def _holding_to_dict(h: FundHoldings) -> Dict:
    return {
        "fund_name": h.fund_name,
        "provider_name": h.provider_name,
        "symbol": h.symbol,
        "share_count": h.share_count,
        "holding_weight": h.holding_weight,
        "market_value": h.market_value,
        "as_of_date": h.as_of_date.isoformat(),
    }


def _dict_to_holding(d: Dict) -> FundHoldings:
    return FundHoldings(
        fund_name=d["fund_name"],
        provider_name=d["provider_name"],
        symbol=d["symbol"],
        share_count=int(d["share_count"]),
        holding_weight=float(d["holding_weight"]),
        market_value=float(d["market_value"]),
        as_of_date=datetime.fromisoformat(d["as_of_date"]),
    )


def _extract_provider_cik(provider: DataProvider) -> Optional[str]:
    """Pull the module-level _*_CIK constant from a 13F provider module."""
    mod = getattr(provider, "__module__", "")
    try:
        import importlib
        module = importlib.import_module(mod)
        for attr in dir(module):
            if attr.endswith("_CIK") and attr.startswith("_"):
                val = getattr(module, attr)
                if isinstance(val, str) and val:
                    return val
    except Exception:
        pass
    return None
