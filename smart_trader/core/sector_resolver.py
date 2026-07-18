"""SectorResolver — fetches and caches stock sectors via yfinance + Supabase.

Replaces the hardcoded DEFAULT_SECTOR_MAP. Sectors are cached in a Supabase
`sectors` table so they survive restarts and don't require repeated yfinance
lookups. Unknown sectors are retried after `retry_days` (default 7).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class SectorResolver:
    """Resolve stock sectors with Supabase cache + yfinance fallback."""

    def __init__(self, retry_days: int = 7):
        from smart_trader.data.supabase_client import SupabaseClient
        self._sb = SupabaseClient()
        self._retry_days = retry_days
        # In-memory cache for the current session
        self._cache: Dict[str, str] = {}
        self._load_from_supabase()

    def _load_from_supabase(self) -> None:
        """Bulk-load all cached sectors from Supabase on startup."""
        rows = self._sb.select(
            "sectors",
            columns="symbol,sector,resolved_at",
            params={"limit": "5000"},
        )
        cutoff = (datetime.now() - timedelta(days=self._retry_days)).isoformat()
        for r in rows:
            sym = r["symbol"]
            sector = r["sector"]
            resolved_at = r.get("resolved_at", "")
            # Keep known sectors; retry "unknown" if stale
            if sector != "unknown" or resolved_at > cutoff:
                self._cache[sym] = sector
        logger.info(f"SectorResolver: loaded {len(self._cache)} sectors from Supabase")

    def get(self, symbol: str) -> str:
        """Return the sector for a symbol. Fetches from yfinance if not cached."""
        if symbol in self._cache:
            return self._cache[symbol]
        sector = self._fetch_from_yfinance(symbol)
        self._cache[symbol] = sector
        self._persist(symbol, sector)
        return sector

    def resolve_batch(self, symbols: Set[str]) -> None:
        """Pre-resolve sectors for a batch of symbols. Skips already-cached."""
        missing = [s for s in symbols if s not in self._cache]
        if not missing:
            return
        logger.info(f"SectorResolver: resolving {len(missing)} new symbols")
        rows_to_persist = []
        for sym in missing:
            sector = self._fetch_from_yfinance(sym)
            self._cache[sym] = sector
            rows_to_persist.append({
                "symbol": sym,
                "sector": sector,
                "resolved_at": datetime.now().isoformat(),
            })
        if rows_to_persist:
            self._sb.upsert("sectors", rows_to_persist, on_conflict="symbol")

    def get_map(self) -> Dict[str, str]:
        """Return the full in-memory sector map (for RiskConfig injection)."""
        return dict(self._cache)

    def _fetch_from_yfinance(self, symbol: str) -> str:
        """Fetch sector from yfinance. Returns lowercase sector or 'unknown'."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.info
            sector = info.get("sector", "unknown") or "unknown"
            sector = sector.lower()
            if sector != "unknown":
                logger.info(f"  Resolved sector for {symbol}: {sector}")
            else:
                logger.debug(f"  Sector unknown for {symbol}")
            return sector
        except Exception as e:
            logger.debug(f"  Sector resolution failed for {symbol}: {e}")
            return "unknown"

    def _persist(self, symbol: str, sector: str) -> None:
        """Write a single sector to Supabase."""
        self._sb.upsert(
            "sectors",
            [{"symbol": symbol, "sector": sector, "resolved_at": datetime.now().isoformat()}],
            on_conflict="symbol",
        )
