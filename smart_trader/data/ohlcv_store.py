"""
OHLCV Store — Supabase-primary cache for price bars.

Read-through cache: check Supabase first, fetch from yfinance on miss,
write back to Supabase. Replaces the previous SQLite-primary implementation.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class OHLCVStore:
    """Supabase-backed OHLCV cache."""

    def __init__(self, db_path: str = "", supabase_sync_enabled: bool = True):
        # db_path kept in signature for backward compat but unused.
        from smart_trader.data.supabase_client import SupabaseClient
        self._sb = SupabaseClient()

    # ------------------------------------------------------------------ main

    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Return OHLCV bars for [start, end]. Read-through cache via Supabase."""
        symbol = symbol.upper().strip()

        cached = self._read_range(symbol, start, end)
        if self._range_covered(cached, start, end):
            logger.debug(f"  ohlcv: cache HIT {symbol} {start}..{end} ({len(cached)} rows)")
            return cached

        logger.info(f"  ohlcv: cache MISS {symbol} {start}..{end}, fetching yfinance")
        df = self._fetch_from_yfinance(symbol, start, end)
        if df.empty:
            logger.warning(f"  ohlcv: yfinance returned empty for {symbol} {start}..{end}")
            return cached  # fall back to whatever we have

        self._write(symbol, df)

        # Re-read from Supabase so the caller gets consistent data.
        return self._read_range(symbol, start, end)

    # ------------------------------------------------------------------ Supabase read

    def _read_range(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        rows = self._sb.select(
            "ohlcv_bars",
            columns="date,open,high,low,close,volume",
            params={
                "symbol": f"eq.{symbol}",
                "date": f"gte.{start}",
                "date": f"lte.{end}",
                "order": "date.asc",
            },
        )
        # PostgREST doesn't support duplicate param keys for AND on same column.
        # Use the combined filter syntax instead.
        rows = self._sb.select(
            "ohlcv_bars",
            columns="date,open,high,low,close,volume",
            params={
                "symbol": f"eq.{symbol}",
                "and": f"(date.gte.{start},date.lte.{end})",
                "order": "date.asc",
            },
        )

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(int)
        return df

    def _range_covered(self, df: pd.DataFrame, start: str, end: str) -> bool:
        if df.empty:
            return False
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        left_ok = df.index[0] <= start_ts + pd.Timedelta(days=3)
        right_ok = df.index[-1] >= end_ts - pd.Timedelta(days=3)
        return bool(left_ok and right_ok)

    # ------------------------------------------------------------------ Supabase write

    def _write(self, symbol: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        now = datetime.now().isoformat()
        rows = [
            {
                "symbol": symbol,
                "date": idx.strftime("%Y-%m-%d"),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
                "ingested_at": now,
            }
            for idx, r in df.iterrows()
        ]
        # Batch in chunks of 500 to avoid request size limits.
        chunk_size = 500
        total = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            ok = self._sb.upsert("ohlcv_bars", chunk, on_conflict="symbol,date")
            if ok:
                total += len(chunk)
        logger.info(f"  ohlcv: wrote {total} rows for {symbol}")

    # ------------------------------------------------------------------ yfinance

    def _fetch_from_yfinance(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start, end=end, auto_adjust=True)
        if df.empty:
            return df
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
        return df
