"""Shared yfinance-backed ETF holdings utility.

Used by MOAT, CGDV, SYLD — and suitable for any publicly-traded ETF whose
holdings yfinance exposes via `Ticker.funds_data.top_holdings`.

Limitations versus full disclosure:
  - yfinance returns the **top 10** holdings only, not the full portfolio.
    For MOAT (~50 stocks), this captures roughly 25-30% of fund weight.
    For concentrated ETFs like CGDV (~40-60 stocks, top-10 is ~40% of NAV),
    coverage is better.
  - `share_count` is not exposed by yfinance for ETF holdings; we set it
    to 0 (the scorer doesn't use share_count — only holding_weight).
  - `market_value` is estimated from `totalAssets * holding_weight`. This
    matches published fund disclosures within a few percent.
  - `as_of_date` is the fetch date since yfinance doesn't expose the
    underlying N-PORT filing date.

Upgrade path: replace with SEC N-PORT parsing for full-portfolio disclosure
(adds ~2 weeks of work for a new XML schema).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from smart_trader.core.smart_money import FundHoldings

logger = logging.getLogger(__name__)


def fetch_etf_top_holdings(
    ticker: str,
    fund_name: str,
    provider_name: str,
) -> List[FundHoldings]:
    """Fetch top-10 holdings for an ETF via yfinance. Returns [] on failure.

    The caller is responsible for the thin DataProvider wrapper. This
    function handles all yfinance-specific quirks.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error(f"{provider_name}: yfinance not installed")
        return []

    try:
        t = yf.Ticker(ticker)
        fd = t.funds_data
        if fd is None:
            logger.warning(f"{provider_name}: no funds_data for {ticker}")
            return []
        top = fd.top_holdings
        if top is None or getattr(top, "empty", True):
            logger.warning(f"{provider_name}: empty top_holdings for {ticker}")
            return []
    except Exception as e:
        logger.warning(f"{provider_name}: yfinance fetch failed for {ticker}: {e}")
        return []

    # AUM for market_value estimation
    total_assets = 0.0
    try:
        info = t.info
        total_assets = float(info.get("totalAssets") or 0)
    except Exception:
        total_assets = 0.0

    now = datetime.now()
    result: List[FundHoldings] = []
    for symbol, row in top.iterrows():
        sym = str(symbol).strip().upper()
        if not sym or not sym.replace(".", "").replace("-", "").isalnum():
            continue
        weight = float(row.get("Holding Percent", 0) or 0)
        if weight <= 0:
            continue
        mv = total_assets * weight if total_assets > 0 else 0.0
        result.append(FundHoldings(
            fund_name=fund_name,
            provider_name=provider_name,
            symbol=sym,
            share_count=0,           # not exposed by yfinance
            holding_weight=weight,
            market_value=mv,
            as_of_date=now,
        ))

    logger.info(
        f"{provider_name}: fetched {len(result)} top holdings for {ticker} "
        f"(AUM ~${total_assets/1e9:.1f}B, top-10 weight sum "
        f"{sum(h.holding_weight for h in result)*100:.1f}%)"
    )
    return result
