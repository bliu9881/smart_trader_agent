"""PortfolioStore — Supabase-backed persistence for Smart_Money_Portfolio snapshots.

Replaces the previous SQLite-primary implementation. All reads and writes go
directly to Supabase via the REST API.

Tables used:
  portfolio_snapshots   — snapshot metadata (snapshot_id, generated_at, top_n_size, universe_size)
  portfolio_stocks      — scored stocks per snapshot
  portfolio_stock_funds — per-fund holdings per stock per snapshot
  fund_holdings_raw     — raw holdings archive (append-only)
  exit_state_kv         — signal-driven exit state (key-value JSON blobs)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from smart_trader.core.portfolio_scorer import ScoredStock
from smart_trader.core.smart_money import FundHoldings

logger = logging.getLogger(__name__)


def _parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp from Supabase.

    Python 3.9's datetime.fromisoformat() doesn't handle timezone offsets
    or truncated fractional seconds that Postgres/Supabase emits (e.g.
    '2026-04-27T19:35:52.15997+00:00'). Strip the tz suffix and parse
    the naive portion, then attach UTC.
    """
    # Remove trailing timezone offset (+00:00, Z, etc.)
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", s).rstrip("Z")
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        # Last resort: drop fractional seconds entirely
        return datetime.fromisoformat(clean.split(".")[0])


_EXIT_STATE_KEYS = (
    "position_open_ts",
    "recent_exits",
    "top_n_history",
    "conviction_below_since",
)


@dataclass
class StoredStock:
    symbol: str
    rank: int
    in_top_n: bool
    composite_score: float
    overlap_count: int
    avg_holding_weight: float
    performance_score: float
    momentum_score: float
    relative_strength: float
    optimal_entry_price: Optional[float]
    funds: List[Dict] = field(default_factory=list)


@dataclass
class PortfolioSnapshot:
    snapshot_id: int
    generated_at: datetime
    top_n_size: int
    universe_size: int
    stocks: List[StoredStock]


class PortfolioStore:
    def __init__(
        self,
        db_path: str = "",
        retention_days: int = 90,
        supabase_sync_enabled: bool = True,
    ):
        # db_path kept in signature for backward compat but unused.
        from smart_trader.data.supabase_client import SupabaseClient
        self._sb = SupabaseClient()
        self.retention_days = retention_days

    # ------------------------------------------------------------------ raw holdings archive

    def store_raw_holdings(self, holdings: List[FundHoldings]) -> int:
        """Upsert raw fund holdings into the fund_holdings_raw table."""
        if not holdings:
            return 0
        now = datetime.now().isoformat()
        rows = [
            {
                "fund_name": h.fund_name,
                "provider_name": h.provider_name,
                "symbol": h.symbol,
                "share_count": int(h.share_count),
                "holding_weight": float(h.holding_weight),
                "market_value": float(h.market_value),
                "as_of_date": h.as_of_date.isoformat()
                    if isinstance(h.as_of_date, datetime) else str(h.as_of_date),
                "fetched_at": now,
            }
            for h in holdings
        ]
        ok = self._sb.upsert(
            "fund_holdings_raw", rows,
            on_conflict="fund_name,symbol,as_of_date",
        )
        count = len(rows) if ok else 0
        logger.info(f"PortfolioStore: upserted {count} rows into fund_holdings_raw")
        return count

    # ------------------------------------------------------------------ write

    def save_snapshot(
        self,
        scored: List[ScoredStock],
        top_n_size: int,
        entry_prices: Dict[str, Optional[float]],
    ) -> int:
        """Persist a full-universe snapshot. Returns snapshot_id."""
        generated_at = datetime.now().isoformat()
        universe_size = len(scored)

        # 1. Insert snapshot metadata and get the generated snapshot_id back.
        result = self._sb.insert(
            "portfolio_snapshots",
            [{
                "generated_at": generated_at,
                "top_n_size": top_n_size,
                "universe_size": universe_size,
            }],
            return_rows=True,
        )
        if not result:
            logger.warning("PortfolioStore: failed to create snapshot row")
            return -1
        snapshot_id = result[0]["snapshot_id"]

        # 2. Insert scored stocks.
        stock_rows = []
        fund_rows = []
        for rank, s in enumerate(scored, start=1):
            in_top_n = rank <= top_n_size and s.composite_score > 0
            stock_rows.append({
                "snapshot_id": snapshot_id,
                "generated_at": generated_at,
                "symbol": s.symbol,
                "rank": rank,
                "in_top_n": in_top_n,
                "composite_score": s.composite_score,
                "overlap_count": s.overlap_count,
                "avg_holding_weight": s.average_holding_weight,
                "performance_score": s.performance_score,
                "momentum_score": s.momentum_score,
                "relative_strength": s.relative_strength,
                "optimal_entry_price": entry_prices.get(s.symbol) if in_top_n else None,
            })
            for f in s.funds:
                as_of = f.as_of_date.isoformat() if isinstance(f.as_of_date, datetime) else str(f.as_of_date)
                fund_rows.append({
                    "snapshot_id": snapshot_id,
                    "symbol": s.symbol,
                    "fund_name": f.fund_name,
                    "provider_name": f.provider_name,
                    "holding_weight": f.holding_weight,
                    "share_count": f.share_count,
                    "market_value": f.market_value,
                    "as_of_date": as_of,
                })

        # Batch stocks in chunks.
        chunk_size = 200
        for i in range(0, len(stock_rows), chunk_size):
            self._sb.insert("portfolio_stocks", stock_rows[i : i + chunk_size])

        if fund_rows:
            for i in range(0, len(fund_rows), chunk_size):
                self._sb.insert("portfolio_stock_funds", fund_rows[i : i + chunk_size])

        # 3. Retention purge.
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).isoformat()
        self._sb.delete("portfolio_snapshots", {"generated_at": f"lt.{cutoff}"})

        logger.info(
            f"PortfolioStore: wrote snapshot_id={snapshot_id} "
            f"(universe={universe_size}, top_n={top_n_size}, "
            f"stocks={len(stock_rows)}, fund_rows={len(fund_rows)})"
        )
        return snapshot_id

    # ------------------------------------------------------------------ exit state

    def save_exit_state(self, state: Dict[str, Dict[str, datetime]]) -> None:
        """Persist signal-driven exit state dicts to exit_state_kv."""
        now_iso = datetime.now().isoformat()
        rows = []
        for key in _EXIT_STATE_KEYS:
            d = state.get(key, {})
            payload = {sym: ts.isoformat() for sym, ts in d.items()}
            rows.append({
                "key": key,
                "value": json.dumps(payload),
                "updated_at": now_iso,
            })
        self._sb.upsert("exit_state_kv", rows, on_conflict="key")

    def load_exit_state(self) -> Dict[str, Dict[str, datetime]]:
        """Return the persisted signal-driven exit state."""
        out: Dict[str, Dict[str, datetime]] = {key: {} for key in _EXIT_STATE_KEYS}
        keys_csv = ",".join(_EXIT_STATE_KEYS)
        rows = self._sb.select(
            "exit_state_kv",
            columns="key,value",
            params={"key": f"in.({keys_csv})"},
        )
        for row in rows:
            key = row.get("key")
            value = row.get("value")
            if key not in _EXIT_STATE_KEYS:
                continue
            try:
                payload = json.loads(value) if isinstance(value, str) else (value or {})
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"  load_exit_state: corrupt JSON for {key}: {e}")
                continue
            inner: Dict[str, datetime] = {}
            for sym, ts_str in payload.items():
                try:
                    inner[sym] = datetime.fromisoformat(ts_str)
                except (TypeError, ValueError) as e:
                    logger.warning(f"  load_exit_state: bad timestamp for {key}/{sym}: {e}")
            out[key] = inner
        return out

    # ------------------------------------------------------------------ read

    def load_latest(self) -> Optional[PortfolioSnapshot]:
        """Return the most recent portfolio snapshot, or None."""
        # 1. Get latest snapshot metadata.
        snap_rows = self._sb.select(
            "portfolio_snapshots",
            columns="snapshot_id,generated_at,top_n_size,universe_size",
            params={"order": "snapshot_id.desc", "limit": "1"},
        )
        if not snap_rows:
            return None
        snap = snap_rows[0]
        snapshot_id = snap["snapshot_id"]

        # 2. Get stocks for this snapshot.
        stock_rows = self._sb.select(
            "portfolio_stocks",
            columns="symbol,rank,in_top_n,composite_score,overlap_count,"
                    "avg_holding_weight,performance_score,momentum_score,"
                    "relative_strength,optimal_entry_price",
            params={
                "snapshot_id": f"eq.{snapshot_id}",
                "order": "rank.asc",
                "limit": "1000",
            },
        )

        # 3. Get fund holdings for this snapshot.
        fund_rows = self._sb.select(
            "portfolio_stock_funds",
            columns="symbol,fund_name,provider_name,holding_weight,"
                    "share_count,market_value,as_of_date",
            params={
                "snapshot_id": f"eq.{snapshot_id}",
                "limit": "5000",
            },
        )

        funds_by_symbol: Dict[str, List[Dict]] = {}
        for r in fund_rows:
            funds_by_symbol.setdefault(r["symbol"], []).append({
                "fund_name": r["fund_name"],
                "provider_name": r["provider_name"],
                "holding_weight": float(r["holding_weight"]),
                "share_count": int(r["share_count"]),
                "market_value": float(r["market_value"]),
                "as_of_date": r["as_of_date"],
            })

        stocks = [
            StoredStock(
                symbol=r["symbol"],
                rank=int(r["rank"]),
                in_top_n=bool(r["in_top_n"]),
                composite_score=float(r["composite_score"]),
                overlap_count=int(r["overlap_count"]),
                avg_holding_weight=float(r["avg_holding_weight"]),
                performance_score=float(r["performance_score"]),
                momentum_score=float(r["momentum_score"]),
                relative_strength=float(r["relative_strength"]),
                optimal_entry_price=(
                    float(r["optimal_entry_price"])
                    if r.get("optimal_entry_price") is not None
                    else None
                ),
                funds=funds_by_symbol.get(r["symbol"], []),
            )
            for r in stock_rows
        ]

        return PortfolioSnapshot(
            snapshot_id=snapshot_id,
            generated_at=_parse_ts(snap["generated_at"]),
            top_n_size=int(snap["top_n_size"]),
            universe_size=int(snap["universe_size"]),
            stocks=stocks,
        )
