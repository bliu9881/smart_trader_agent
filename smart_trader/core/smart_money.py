"""
Smart Money Scanner — multi-source stock selection layer.

Aggregates trade filings from four pluggable data providers (Capitol Trades,
Berkshire 13F, ARK Invest, Insider Cluster), ranks candidates by conviction
scoring, applies regime-based filtering, and feeds candidates into the
existing signal pipeline.

Smart money tells the system WHAT to buy; the HMM tells it WHEN.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from smart_trader.settings.config import RiskConfig, SmartMoneyConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TradeFiling:
    """Normalized record from any data provider."""
    source: str              # "capitol_trades" | "berkshire_13f" | "ark_invest" | "insider_cluster"
    actor: str               # politician name, "Berkshire Hathaway", fund name, insider name+title
    symbol: str              # ticker symbol (uppercase)
    tx_type: str             # "buy" | "sell" | "increase" | "decrease"
    dollar_amount: Optional[float]   # estimated dollar amount (None if only shares known)
    share_change: Optional[int]      # share delta (None if only dollars known)
    filing_date: datetime    # when the filing was disclosed
    trade_date: datetime     # when the actual trade occurred


@dataclass
class CandidateSymbol:
    """A ranked candidate from the smart money pipeline."""
    symbol: str
    conviction_score: float
    sources: List[str]       # e.g. ["capitol_trades", "insider_cluster"]
    actors: List[str]        # e.g. ["Nancy Pelosi", "CEO John Smith"]
    total_dollar_volume: float
    filing_count: int
    most_recent_filing: datetime


@dataclass
class FundHoldings:
    """Normalized snapshot record emitted by Holdings_Providers.

    Represents a single stock position held by a fund at a point in time.
    Distinct from TradeFiling (a delta) — this is a point-in-time snapshot
    used by the Smart_Money_Portfolio feature.
    """
    fund_name: str           # e.g. "Berkshire Hathaway", "ARK Innovation ETF (ARKK)"
    provider_name: str       # e.g. "berkshire_13f", "ark_invest"
    symbol: str              # ticker symbol (uppercase)
    share_count: int
    holding_weight: float    # fund's portfolio percentage (0.0–1.0), e.g. 0.40 = 40%
    market_value: float      # USD
    as_of_date: datetime     # underlying filing or disclosure date


@dataclass
class CacheEntry:
    """In-memory cache entry for a provider's data."""
    provider_name: str
    filings: List[TradeFiling]
    fetched_at: datetime
    ttl_hours: float


# ---------------------------------------------------------------------------
# DataProvider ABC
# ---------------------------------------------------------------------------

class DataProvider(ABC):
    """Interface for smart money data sources.

    All providers emit TradeFiling deltas via fetch_raw_data + parse_filings.
    Holdings-capable providers (Berkshire, ARK, Pershing Square, Appaloosa,
    Duquesne) additionally implement fetch_holdings() to emit FundHoldings
    snapshots for the Smart_Money_Portfolio feature.
    """

    @abstractmethod
    def fetch_raw_data(self) -> str:
        """Fetch raw data from the external source. Returns raw text/HTML."""

    @abstractmethod
    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        """Parse raw data into normalized TradeFiling records."""

    @abstractmethod
    def get_cache_ttl_hours(self) -> float:
        """Return the natural refresh cadence for this provider in hours."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique identifier for this provider (used as cache key)."""

    def fetch_holdings(self) -> List[FundHoldings]:
        """Return the current portfolio snapshot for this provider.

        Default: returns []. Delta-only providers (Capitol Trades, SEC Form 4,
        Insider Cluster) don't have an associated portfolio snapshot; they keep
        the default. Holdings-capable providers override this.
        """
        return []

    def refresh_mode(self) -> Literal["daily", "filing_detect"]:
        """Refresh cadence hint for the Holdings_Scraper.

        - "daily":          fetch fresh on the first cycle of each NY trading day.
        - "filing_detect":  poll the filing index daily, re-parse only when a
                            new accession number appears.
        Default is "daily". 13F providers override with "filing_detect".
        """
        return "daily"


# ---------------------------------------------------------------------------
# SmartMoneyScanner — orchestrates providers, ranking, scoring, filtering
# ---------------------------------------------------------------------------

class SmartMoneyScanner:
    """Aggregates smart money data, ranks candidates, applies regime filtering."""

    def __init__(self, config: SmartMoneyConfig, risk_config: RiskConfig):
        from smart_trader.core.smart_money_providers._provider_health import (
            ProviderHealthTracker,
        )

        self.config = config
        self.risk_config = risk_config
        self._providers: List[DataProvider] = []
        self._cache: Dict[str, CacheEntry] = {}  # provider_name → CacheEntry
        self._init_providers()
        self._restore_disk_cache()
        self._health = ProviderHealthTracker(
            cache_dir=Path(self.config.disk_cache_dir),
            max_empty_days=dict(self.config.provider_health_max_empty_days),
        )

    # ------------------------------------------------------------------
    # Provider initialization
    # ------------------------------------------------------------------

    def _init_providers(self) -> None:
        """Instantiate enabled providers based on config toggles."""
        from smart_trader.core.smart_money_providers.capitol_trades import CapitolTradesProvider
        from smart_trader.core.smart_money_providers.sec_edgar import SECEdgarProvider
        from smart_trader.core.smart_money_providers.berkshire_13f import BerkshireProvider
        from smart_trader.core.smart_money_providers.ark_invest import ARKProvider
        from smart_trader.core.smart_money_providers.insider_cluster import InsiderClusterProvider

        if self.config.capitol_trades_enabled:
            self._providers.append(CapitolTradesProvider(self.config))
        if self.config.sec_edgar_enabled:
            self._providers.append(SECEdgarProvider(self.config))
        if self.config.berkshire_enabled:
            self._providers.append(BerkshireProvider(self.config))
        if self.config.ark_enabled:
            self._providers.append(ARKProvider(self.config))
        if self.config.insider_cluster_enabled:
            self._providers.append(InsiderClusterProvider(self.config))

        logger.info(
            f"SmartMoneyScanner: initialized {len(self._providers)} providers: "
            f"{[p.provider_name for p in self._providers]}"
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _fetch_or_cache(self, provider: DataProvider) -> List[TradeFiling]:
        """Return cached filings if within TTL, else fetch fresh and update cache."""
        name = provider.provider_name
        now = datetime.now()

        # Check in-memory cache
        entry = self._cache.get(name)
        if entry is not None:
            age_hours = (now - entry.fetched_at).total_seconds() / 3600
            if age_hours < entry.ttl_hours:
                logger.info(f"  {name}: cache HIT (age {age_hours:.1f}h < TTL {entry.ttl_hours:.0f}h)")
                return entry.filings

        # Cache miss — fetch fresh data
        logger.info(f"  {name}: cache MISS, fetching fresh data...")
        try:
            raw = provider.fetch_raw_data()
            filings = provider.parse_filings(raw)
        except Exception as e:
            logger.error(f"  {name}: fetch/parse failed: {e}")
            # The fresh attempt failed; record it as an empty fetch so the
            # health tracker sees the streak even when stale cache covers
            # the caller. Health snapshots reflect reality, not cache cushions.
            self._health.record_fetch(name, 0)
            if entry is not None:
                logger.info(f"  {name}: falling back to stale cache ({len(entry.filings)} filings)")
                return entry.filings
            return []

        # Record the fresh fetch outcome before any caching/IO so a write
        # failure does not lose the observation.
        self._health.record_fetch(name, len(filings))

        # Update in-memory cache
        ttl = provider.get_cache_ttl_hours()
        self._cache[name] = CacheEntry(
            provider_name=name,
            filings=filings,
            fetched_at=now,
            ttl_hours=ttl,
        )

        # Write to disk
        self._write_disk_cache(name, filings)

        return filings

    def _write_disk_cache(self, provider_name: str, filings: List[TradeFiling]) -> None:
        """Write filings to JSON file in disk_cache_dir."""
        try:
            cache_dir = Path(self.config.disk_cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)

            cache_file = cache_dir / f"{provider_name}.json"
            data = {
                "provider": provider_name,
                "fetched_at": datetime.now().isoformat(),
                "ttl_hours": self._cache.get(provider_name, CacheEntry(
                    provider_name=provider_name, filings=[], fetched_at=datetime.now(), ttl_hours=24.0
                )).ttl_hours,
                "filings": [
                    {
                        "source": f.source,
                        "actor": f.actor,
                        "symbol": f.symbol,
                        "tx_type": f.tx_type,
                        "dollar_amount": f.dollar_amount,
                        "share_change": f.share_change,
                        "filing_date": f.filing_date.isoformat(),
                        "trade_date": f.trade_date.isoformat(),
                    }
                    for f in filings
                ],
            }
            cache_file.write_text(json.dumps(data, indent=2))
            logger.debug(f"  Wrote {len(filings)} filings to {cache_file}")
        except Exception as e:
            logger.warning(f"  Failed to write disk cache for {provider_name}: {e}")

    def _restore_disk_cache(self) -> None:
        """On startup, load cache files that are within TTL."""
        cache_dir = Path(self.config.disk_cache_dir)
        if not cache_dir.exists():
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"SmartMoneyScanner: cannot create cache dir {cache_dir}: {e}")
            return

        # Build a TTL lookup from provider names
        ttl_map = {
            "capitol_trades": self.config.capitol_trades_cache_ttl,
            "sec_edgar": self.config.sec_edgar_cache_ttl,
            "berkshire_13f": self.config.berkshire_cache_ttl,
            "ark_invest": self.config.ark_cache_ttl,
            "insider_cluster": self.config.insider_cluster_cache_ttl,
        }

        for cache_file in cache_dir.glob("*.json"):
            if cache_file.name == "candidates.json":
                continue
            try:
                data = json.loads(cache_file.read_text())
                provider_name = data.get("provider", cache_file.stem)
                fetched_at_str = data.get("fetched_at", "")
                ttl_hours = data.get("ttl_hours", ttl_map.get(provider_name, 24.0))

                fetched_at = datetime.fromisoformat(fetched_at_str)
                age_hours = (datetime.now() - fetched_at).total_seconds() / 3600

                if age_hours >= ttl_hours:
                    logger.info(f"  Disk cache {cache_file.name}: stale (age {age_hours:.1f}h >= TTL {ttl_hours:.0f}h), discarding")
                    continue

                filings = []
                for f_data in data.get("filings", []):
                    try:
                        filings.append(TradeFiling(
                            source=f_data["source"],
                            actor=f_data["actor"],
                            symbol=f_data["symbol"],
                            tx_type=f_data["tx_type"],
                            dollar_amount=f_data.get("dollar_amount"),
                            share_change=f_data.get("share_change"),
                            filing_date=datetime.fromisoformat(f_data["filing_date"]),
                            trade_date=datetime.fromisoformat(f_data["trade_date"]),
                        ))
                    except Exception:
                        continue

                self._cache[provider_name] = CacheEntry(
                    provider_name=provider_name,
                    filings=filings,
                    fetched_at=fetched_at,
                    ttl_hours=ttl_hours,
                )
                logger.info(f"  Disk cache {cache_file.name}: restored {len(filings)} filings (age {age_hours:.1f}h)")

            except Exception as e:
                logger.warning(f"  Disk cache {cache_file.name}: corrupt, discarding ({e})")

    # ------------------------------------------------------------------
    # Politician ranking
    # ------------------------------------------------------------------

    def _rank_politicians(self, filings: List[TradeFiling]) -> List[str]:
        """Rank Capitol Trades politicians by filing count + dollar volume.

        Returns top N politician names.
        """
        # Filter to Capitol Trades BUY filings within recency window
        cutoff = datetime.now() - timedelta(days=self.config.recency_window_days)
        ct_buys = [
            f for f in filings
            if f.source == "capitol_trades"
            and f.tx_type in ("buy", "increase")
            and f.trade_date >= cutoff
        ]

        # Aggregate per politician
        politician_stats: Dict[str, Dict[str, Any]] = {}
        for f in ct_buys:
            if f.actor not in politician_stats:
                politician_stats[f.actor] = {"count": 0, "dollar_volume": 0.0}
            politician_stats[f.actor]["count"] += 1
            politician_stats[f.actor]["dollar_volume"] += f.dollar_amount or 0.0

        # Filter by minimum filings
        qualified = {
            name: stats for name, stats in politician_stats.items()
            if stats["count"] >= self.config.min_politician_filings
        }

        # Sort by (filing_count, dollar_volume) descending
        ranked = sorted(
            qualified.items(),
            key=lambda x: (x[1]["count"], x[1]["dollar_volume"]),
            reverse=True,
        )

        top_names = [name for name, _ in ranked[:self.config.top_n_politicians]]
        logger.info(
            f"  Politician ranking: {len(politician_stats)} total, "
            f"{len(qualified)} qualified, top {len(top_names)} selected"
        )
        return top_names

    # ------------------------------------------------------------------
    # Conviction scoring
    # ------------------------------------------------------------------

    def _compute_conviction_scores(
        self,
        filings: List[TradeFiling],
        tx_filter: tuple = ("buy", "increase"),
    ) -> List[CandidateSymbol]:
        """Aggregate filings per symbol, compute Conviction_Score, sort descending.

        tx_filter selects which transaction types qualify. Default ("buy",
        "increase") drives entry candidates. Pass ("sell", "decrease") to
        score exit candidates on the same recency / threshold rules.
        """
        now = datetime.now()
        cutoff = now - timedelta(days=self.config.recency_window_days)
        berkshire_cutoff = now - timedelta(days=self.config.berkshire_recency_days)

        relevant_filings = [f for f in filings if f.tx_type in tx_filter]

        # Apply dollar amount / share change thresholds and recency
        qualified: List[TradeFiling] = []
        for f in relevant_filings:
            # Recency filter
            if f.source == "berkshire_13f":
                if f.trade_date < berkshire_cutoff:
                    continue
            else:
                if f.trade_date < cutoff:
                    continue

            # Dollar / share threshold
            if f.source == "ark_invest":
                if f.share_change is not None and f.share_change < self.config.min_share_change:
                    continue
            else:
                if f.dollar_amount is not None and f.dollar_amount < self.config.min_trade_amount:
                    continue

            qualified.append(f)

        # Group by symbol
        symbol_data: Dict[str, Dict[str, Any]] = {}
        for f in qualified:
            if f.symbol not in symbol_data:
                symbol_data[f.symbol] = {
                    "sources": set(),
                    "actors": set(),
                    "dollar_volume": 0.0,
                    "filing_count": 0,
                    "most_recent": f.filing_date,
                }
            sd = symbol_data[f.symbol]
            sd["sources"].add(f.source)
            sd["actors"].add(f.actor)
            sd["dollar_volume"] += f.dollar_amount or 0.0
            sd["filing_count"] += 1
            if f.filing_date > sd["most_recent"]:
                sd["most_recent"] = f.filing_date

        # Compute conviction scores
        candidates: List[CandidateSymbol] = []
        for symbol, sd in symbol_data.items():
            total_dollar = sd["dollar_volume"]
            days_since = max(0, (now - sd["most_recent"]).days)

            score = _compute_conviction_score(
                sources=sd["sources"],
                n_actors=len(sd["actors"]),
                total_dollar_volume=total_dollar,
                filing_count=sd["filing_count"],
                days_since_most_recent=days_since,
                cfg=self.config,
            )

            candidates.append(CandidateSymbol(
                symbol=symbol,
                conviction_score=score,
                sources=sorted(sd["sources"]),
                actors=sorted(sd["actors"]),
                total_dollar_volume=total_dollar,
                filing_count=sd["filing_count"],
                most_recent_filing=sd["most_recent"],
            ))

        # Sort by conviction score descending
        candidates.sort(key=lambda c: c.conviction_score, reverse=True)
        return candidates

    # ------------------------------------------------------------------
    # Regime filtering
    # ------------------------------------------------------------------

    def _apply_regime_filter(
        self, candidates: List[CandidateSymbol], vol_rank: float
    ) -> List[CandidateSymbol]:
        """Filter candidates based on vol_rank thresholds."""
        if vol_rank <= 0.33:
            # Low vol: accept all candidates
            return candidates
        elif vol_rank <= 0.67:
            # Mid vol: only defensive symbols
            defensive = set(self.config.defensive_symbols)
            return [c for c in candidates if c.symbol in defensive]
        else:
            # High vol: suppress all smart money candidates
            return []

    # ------------------------------------------------------------------
    # Sector resolution
    # ------------------------------------------------------------------

    def _resolve_sector(self, symbol: str) -> str:
        """Resolve sector for a symbol not in the sector map. Returns sector string."""
        if symbol in self.risk_config.sector_map:
            return self.risk_config.sector_map[symbol]

        sector = "unknown"
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.info
            sector = info.get("sector", "unknown") or "unknown"
            sector = sector.lower()
        except Exception as e:
            logger.debug(f"  Sector resolution failed for {symbol}: {e}")

        self.risk_config.sector_map[symbol] = sector
        logger.info(f"  Resolved sector for {symbol}: {sector}")
        return sector

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def get_candidates(self, vol_rank: float) -> List[CandidateSymbol]:
        """
        Main entry point. Fetches/caches data from all enabled providers,
        ranks politicians, computes conviction scores, applies regime filter.
        Returns sorted candidate list (highest conviction first).
        """
        if not self.config.smart_money_enabled:
            return []

        logger.info("SmartMoneyScanner: evaluating candidates...")

        # Fetch from all providers
        all_filings: List[TradeFiling] = []
        filings_per_provider: Dict[str, int] = {}

        for provider in self._providers:
            filings = self._fetch_or_cache(provider)
            filings_per_provider[provider.provider_name] = len(filings)
            all_filings.extend(filings)

        if not all_filings:
            logger.info("SmartMoneyScanner: no filings from any provider")
            return []

        # Apply politician ranking to Capitol Trades filings
        top_politicians = self._rank_politicians(all_filings)
        if top_politicians:
            # Filter Capitol Trades filings to only top-ranked politicians
            filtered_filings = []
            top_set = set(top_politicians)
            for f in all_filings:
                if f.source == "capitol_trades":
                    if f.actor in top_set:
                        filtered_filings.append(f)
                else:
                    filtered_filings.append(f)
            all_filings = filtered_filings

        # Compute conviction scores
        candidates = self._compute_conviction_scores(all_filings)

        # Apply regime filter
        pre_filter_count = len(candidates)
        candidates = self._apply_regime_filter(candidates, vol_rank)

        # Resolve sectors for new candidates
        for c in candidates:
            self._resolve_sector(c.symbol)

        # Write candidate snapshot
        self._write_candidate_snapshot(candidates)

        # Fire async Supabase sync if enabled
        if self.config.supabase_enabled:
            self._fire_supabase_sync(all_filings, candidates, vol_rank)

        # Log summary
        logger.info(
            f"SmartMoneyScanner: filings={filings_per_provider}, "
            f"candidates={pre_filter_count} → {len(candidates)} after regime filter "
            f"(vol_rank={vol_rank:.2f})"
        )

        # Surface providers that have been silent past their threshold.
        for name, reason in self._health.degraded_providers():
            logger.warning(f"Provider health: {name} — {reason}")

        return candidates

    # ------------------------------------------------------------------
    # Exit candidate detection
    # ------------------------------------------------------------------

    def get_held_position_conviction(
        self, held_symbols
    ) -> Optional[Dict[str, float]]:
        """Compute current buy-side conviction for each held symbol.

        Returns:
          * `None` if no provider produced or served any filings this cycle
            — caller treats this as an outage, not as a portfolio-wide
            decay. Same skip-on-missing-data principle as the empty-top-N
            handling in Phase 2b.
          * `{sym: score}` otherwise, with `0.0` for held symbols that have
            no qualifying recent BUY/INCREASE filings — i.e. genuinely
            decayed conviction.

        The returned dict keys are exactly `held_symbols` (preserving the
        caller's casing). Symbols outside `held_symbols` are excluded.
        """
        if not self.config.smart_money_enabled or not held_symbols:
            return None

        held_upper = {s.upper() for s in held_symbols}

        all_filings: List[TradeFiling] = []
        any_provider_data = False
        for provider in self._providers:
            try:
                filings = self._fetch_or_cache(provider)
            except Exception as e:
                logger.warning(
                    f"  conviction-scan: provider {provider.provider_name} failed: {e}"
                )
                continue
            # _fetch_or_cache always returns a list, even on failure (falls
            # back to stale cache or [] on cold-cache failure). A non-empty
            # cached list counts as data; an empty one means this provider
            # had nothing to contribute, but that alone isn't an outage.
            any_provider_data = any_provider_data or bool(filings)
            all_filings.extend(filings)

        if not any_provider_data:
            return None

        held_filings = [f for f in all_filings if f.symbol.upper() in held_upper]
        candidates = self._compute_conviction_scores(
            held_filings, tx_filter=("buy", "increase")
        )
        score_by_upper = {c.symbol.upper(): c.conviction_score for c in candidates}
        return {s: score_by_upper.get(s.upper(), 0.0) for s in held_symbols}

    def get_held_position_sells(self, held_symbols) -> List[CandidateSymbol]:
        """Score SELL/DECREASE filings on currently held positions.

        Symmetric to get_candidates(): same recency, threshold, and
        conviction-score machinery, but filters tx_type to ("sell",
        "decrease") and restricts to held_symbols. Skips politician ranking
        — the held set is small and we want max recall on sells.

        Returns ranked CandidateSymbols (highest conviction = strongest sell
        signal). Used by the trading loop to detect when a smart-money
        source the bot trusted to enter on has now flagged an exit.
        """
        if not self.config.smart_money_enabled or not held_symbols:
            return []

        held_upper = {s.upper() for s in held_symbols}

        all_filings: List[TradeFiling] = []
        for provider in self._providers:
            try:
                filings = self._fetch_or_cache(provider)
            except Exception as e:
                logger.warning(
                    f"  exit-scan: provider {provider.provider_name} failed: {e}"
                )
                continue
            all_filings.extend(filings)

        held_filings = [f for f in all_filings if f.symbol.upper() in held_upper]
        if not held_filings:
            return []

        return self._compute_conviction_scores(
            held_filings, tx_filter=("sell", "decrease")
        )

    # ------------------------------------------------------------------
    # Disk candidate snapshot
    # ------------------------------------------------------------------

    def _write_candidate_snapshot(self, candidates: List[CandidateSymbol]) -> None:
        """Write candidate list to disk as JSON."""
        try:
            cache_dir = Path(self.config.disk_cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)

            snapshot = {
                "generated_at": datetime.now().isoformat(),
                "candidates": [
                    {
                        "symbol": c.symbol,
                        "conviction_score": c.conviction_score,
                        "sources": c.sources,
                        "actors": c.actors,
                        "total_dollar_volume": c.total_dollar_volume,
                        "filing_count": c.filing_count,
                        "most_recent_filing": c.most_recent_filing.isoformat(),
                    }
                    for c in candidates
                ],
            }
            (cache_dir / "candidates.json").write_text(json.dumps(snapshot, indent=2))
        except Exception as e:
            logger.warning(f"Failed to write candidate snapshot: {e}")

    # ------------------------------------------------------------------
    # Supabase sync (fire-and-forget via threading)
    # ------------------------------------------------------------------

    def _fire_supabase_sync(
        self,
        filings: List[TradeFiling],
        candidates: List[CandidateSymbol],
        vol_rank: float,
    ) -> None:
        """Fire async Supabase sync in background threads. Never blocks."""
        t1 = threading.Thread(
            target=self._sync_to_supabase, args=(filings,), daemon=True
        )
        t2 = threading.Thread(
            target=self._sync_candidates_to_supabase,
            args=(candidates, vol_rank),
            daemon=True,
        )
        t1.start()
        t2.start()

    def _sync_to_supabase(self, filings: List[TradeFiling]) -> None:
        """Fire-and-forget write to Supabase smart_money_filings table."""
        try:
            from smart_trader.settings.credentials import load_credentials
            creds = load_credentials()
            url = creds.get("supabase_url", "")
            key = creds.get("supabase_key", "")
            if not url or not key:
                logger.debug("Supabase sync: no credentials configured")
                return

            import requests as req
            headers = {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            rows = [
                {
                    "source": f.source,
                    "actor": f.actor,
                    "symbol": f.symbol,
                    "tx_type": f.tx_type,
                    "dollar_amount": f.dollar_amount,
                    "share_change": f.share_change,
                    "filing_date": f.filing_date.isoformat(),
                    "trade_date": f.trade_date.isoformat(),
                    "ingested_at": datetime.now().isoformat(),
                }
                for f in filings
            ]
            if rows:
                resp = req.post(
                    f"{url}/rest/v1/smart_money_filings",
                    headers=headers,
                    json=rows,
                    timeout=30,
                )
                if resp.status_code >= 400:
                    logger.warning(f"Supabase filings sync failed: {resp.status_code} {resp.text[:200]}")
                else:
                    logger.debug(f"Supabase: synced {len(rows)} filings")
        except Exception as e:
            logger.warning(f"Supabase filings sync error: {e}")

    def _sync_candidates_to_supabase(
        self, candidates: List[CandidateSymbol], vol_rank: float
    ) -> None:
        """Fire-and-forget write of candidate snapshot to Supabase."""
        try:
            from smart_trader.settings.credentials import load_credentials
            creds = load_credentials()
            url = creds.get("supabase_url", "")
            key = creds.get("supabase_key", "")
            if not url or not key:
                return

            import requests as req
            headers = {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            now = datetime.now().isoformat()
            rows = [
                {
                    "symbol": c.symbol,
                    "conviction_score": c.conviction_score,
                    "sources": c.sources,
                    "generated_at": now,
                    "vol_rank": vol_rank,
                }
                for c in candidates
            ]
            if rows:
                resp = req.post(
                    f"{url}/rest/v1/smart_money_candidates",
                    headers=headers,
                    json=rows,
                    timeout=30,
                )
                if resp.status_code >= 400:
                    logger.warning(f"Supabase candidates sync failed: {resp.status_code} {resp.text[:200]}")
                else:
                    logger.debug(f"Supabase: synced {len(rows)} candidates")
        except Exception as e:
            logger.warning(f"Supabase candidates sync error: {e}")


# ---------------------------------------------------------------------------
# Conviction score formula (module-level for testability)
# ---------------------------------------------------------------------------

def _compute_conviction_score(
    sources,
    n_actors: int,
    total_dollar_volume: float,
    filing_count: int,
    days_since_most_recent: int,
    cfg,
) -> float:
    """Agreement-driven conviction score.

    conviction = source_quality + cluster_bonus + dollar_bonus
               + accumulation_bonus + recency_bonus

    - source_quality: Σ per-source weights over the DISTINCT sources. Rewards
      corroboration across independent source TYPES and bakes in source
      reliability (insiders > congress / 13F > momentum ETFs). Share-only
      sources (13F / ARK) are no longer zeroed out the way a raw log10(dollars)
      term zeroed them — they contribute via quality + clustering.
    - cluster_bonus: rewards multiple distinct ACTORS buying the same name —
      the strongest documented conviction signal (insider-cluster effect).
    - dollar_bonus: tamed, capped modifier so one large disclosed figure can't
      dominate genuine multi-actor agreement. Zero for share-only sources.
    - accumulation_bonus: repeated qualifying filings = building a position.
    - recency_bonus: linear decay over cfg.conviction_recency_days.

    All coefficients live in SmartMoneyConfig for tuning.
    """
    source_quality = sum(
        cfg.conviction_source_weights.get(s, cfg.conviction_default_source_weight)
        for s in sources
    )
    cluster_bonus = min(
        cfg.conviction_cluster_cap,
        cfg.conviction_cluster_per_actor * max(0, n_actors - 1),
    )
    if total_dollar_volume > 0:
        excess = max(0.0, math.log10(total_dollar_volume + 1) - cfg.conviction_dollar_floor_log)
        dollar_bonus = min(cfg.conviction_dollar_cap, cfg.conviction_dollar_coef * excess)
    else:
        dollar_bonus = 0.0
    accum_bonus = min(
        cfg.conviction_accum_cap,
        cfg.conviction_accum_per_filing * max(0, filing_count - 1),
    )
    recency_days = cfg.conviction_recency_days
    recency_bonus = max(0.0, (recency_days - days_since_most_recent) / recency_days)
    return source_quality + cluster_bonus + dollar_bonus + accum_bonus + recency_bonus
