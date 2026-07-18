"""
ARK Invest Provider — Cathie Wood's ETF daily holdings changes.

Uses the public arkfunds.io API (a third-party mirror of ARK's own feeds).
ARK Invest stopped publishing direct CSV downloads from their WordPress
site in early 2025 (URLs now 404 or are JS-rendered behind a download
flow), so arkfunds.io is the most reliable zero-auth source.

API docs: https://arkfunds.io/api
  GET /api/v2/etf/holdings?symbol=ARKK   → current holdings snapshot
  GET /api/v2/etf/trades?symbol=ARKK&date_from=YYYY-MM-DD → trade deltas
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests

from smart_trader.core.smart_money import DataProvider, FundHoldings, TradeFiling
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)

# ETFs tracked. arkfunds.io mirrors every ARK ETF; we target the active
# thematic funds that most closely match what the original CSV feeds covered.
_ARK_FUNDS = ("ARKK", "ARKW", "ARKG", "ARKF", "ARKQ")

_API_HOLDINGS = "https://arkfunds.io/api/v2/etf/holdings"
_API_TRADES = "https://arkfunds.io/api/v2/etf/trades"
_API_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "SmartTrader/1.0 (research@example.com)",
}


class ARKProvider(DataProvider):
    """Fetches ARK Invest daily holdings/trades data."""

    def __init__(self, config: SmartMoneyConfig):
        self.config = config

    @property
    def provider_name(self) -> str:
        return "ark_invest"

    def get_cache_ttl_hours(self) -> float:
        return self.config.ark_cache_ttl

    def refresh_mode(self) -> str:
        return "daily"

    def fetch_holdings(self) -> List[FundHoldings]:
        """Fetch per-ETF current holdings via arkfunds.io.

        One FundHoldings per (fund, ticker) pair — the scorer aggregates at
        the ticker level. arkfunds.io weight is in percent units (e.g. 9.66
        for 9.66%); we convert to fraction for FundHoldings.holding_weight.
        """
        holdings: List[FundHoldings] = []
        for fund_code in _ARK_FUNDS:
            try:
                resp = requests.get(
                    _API_HOLDINGS,
                    params={"symbol": fund_code},
                    headers=_API_HEADERS,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"ARK {fund_code} holdings fetch failed: {e}")
                continue

            fund_rows = data.get("holdings") or []
            for row in fund_rows:
                fh = _row_to_fund_holdings(row, fund_code, self.provider_name)
                if fh is not None:
                    holdings.append(fh)

        logger.info(
            f"ARK Invest: fetched {len(holdings)} holdings across {len(_ARK_FUNDS)} ETFs"
        )
        return holdings

    def fetch_raw_data(self) -> str:
        """Fetch recent trades from arkfunds.io across all tracked ETFs.

        Returns a JSON string of the combined `trades` arrays. The response
        shape (``[{fund, date, direction, ticker, shares, ...}, ...]``) is
        also what `parse_filings` expects, so unit tests can feed a synthetic
        JSON payload without touching the network.
        """
        cutoff = datetime.now() - timedelta(days=self.config.recency_window_days)
        date_from = cutoff.strftime("%Y-%m-%d")
        all_trades: List[Dict[str, Any]] = []
        for fund_code in _ARK_FUNDS:
            try:
                resp = requests.get(
                    _API_TRADES,
                    params={"symbol": fund_code, "date_from": date_from},
                    headers=_API_HEADERS,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                all_trades.extend(data.get("trades") or [])
            except Exception as e:
                logger.warning(f"ARK {fund_code} trades fetch failed: {e}")
                continue
        return json.dumps(all_trades)

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        """Parse a JSON payload of trade dicts into TradeFiling records.

        Applies `min_ark_buying_days`: a ticker is only kept if ARK bought it
        on at least that many distinct days within the recency window.
        """
        if not raw_data:
            return []
        try:
            payload = json.loads(raw_data)
        except Exception as e:
            logger.warning(f"ARK Invest: malformed JSON payload: {e}")
            return []

        # Payload may be either the flat list we produce in fetch_raw_data,
        # or a single arkfunds.io response dict with a "trades" key.
        if isinstance(payload, dict):
            trades = payload.get("trades") or []
        elif isinstance(payload, list):
            trades = payload
        else:
            return []

        filings: List[TradeFiling] = []
        cutoff = datetime.now() - timedelta(days=self.config.recency_window_days)
        symbol_buy_days: Dict[str, set] = {}
        symbol_filings: Dict[str, List[TradeFiling]] = {}

        for row in trades:
            try:
                direction = (row.get("direction") or "").strip().lower()
                if direction != "buy":
                    continue
                ticker = (row.get("ticker") or "").strip().upper()
                if not ticker or len(ticker) > 6 or not ticker.isalpha():
                    continue
                date_str = (row.get("date") or "").strip()
                trade_date = _parse_ark_date(date_str)
                if trade_date is None or trade_date < cutoff:
                    continue
                try:
                    shares = int(row.get("shares") or 0)
                except (TypeError, ValueError):
                    continue
                if shares <= 0:
                    continue
                fund = (row.get("fund") or "").strip() or "ARK"

                symbol_buy_days.setdefault(ticker, set()).add(date_str)
                symbol_filings.setdefault(ticker, []).append(TradeFiling(
                    source="ark_invest",
                    actor=f"ARK {fund}",
                    symbol=ticker,
                    tx_type="buy",
                    dollar_amount=None,
                    share_change=shares,
                    filing_date=trade_date,
                    trade_date=trade_date,
                ))
            except Exception as e:
                logger.debug(f"ARK Invest: skipping malformed trade row: {e}")
                continue

        for symbol, buy_days in symbol_buy_days.items():
            if len(buy_days) >= self.config.min_ark_buying_days:
                filings.extend(symbol_filings[symbol])

        logger.info(
            f"ARK Invest: parsed {len(filings)} trade filings "
            f"({len(symbol_buy_days)} symbols, "
            f"{sum(1 for d in symbol_buy_days.values() if len(d) >= self.config.min_ark_buying_days)} "
            f"passed min buying days filter)"
        )
        return filings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ark_date(date_str: str):
    """Parse ARK date formats (API returns YYYY-MM-DD, legacy CSVs vary)."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _row_to_fund_holdings(row: dict, fund_code: str, provider_name: str):
    """Convert an arkfunds.io holdings row to FundHoldings. None on malformed rows."""
    try:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker or len(ticker) > 6 or not ticker.replace(".", "").isalpha():
            return None
        shares = int(row.get("shares") or 0)
        if shares <= 0:
            return None
        market_value = float(row.get("market_value") or 0.0)
        if market_value <= 0:
            return None
        # API returns weight as percent (e.g. 9.66 for 9.66%)
        weight_pct = float(row.get("weight") or 0.0) / 100.0
        date_str = (row.get("date") or "").strip()
        as_of = _parse_ark_date(date_str) or datetime.now()
        return FundHoldings(
            fund_name=f"ARK {fund_code}",
            provider_name=provider_name,
            symbol=ticker,
            share_count=shares,
            holding_weight=weight_pct,
            market_value=market_value,
            as_of_date=as_of,
        )
    except Exception as e:
        logger.debug(f"ARK {fund_code}: skipping malformed row: {e}")
        return None
