"""VanEck Morningstar Wide Moat ETF (MOAT) provider.

Rule-based: holds the 40-60 cheapest wide-moat US stocks per Morningstar's
analyst team. Quarterly rebalance. Captures the "smart money via committee"
signal — hundreds of Morningstar equity analysts vote on moats.

Uses yfinance for top-10 holdings disclosure. Full-portfolio upgrade path
is SEC N-PORT parsing.
"""
from __future__ import annotations

import logging
from typing import List

from smart_trader.core.smart_money import DataProvider, FundHoldings, TradeFiling
from smart_trader.core.smart_money_providers._etf_yfinance import fetch_etf_top_holdings
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)


class MOATProvider(DataProvider):
    def __init__(self, config: SmartMoneyConfig):
        self.config = config

    @property
    def provider_name(self) -> str:
        return "moat_etf"

    def get_cache_ttl_hours(self) -> float:
        return self.config.moat_cache_ttl

    def refresh_mode(self) -> str:
        return "daily"

    def fetch_raw_data(self) -> str:
        return ""

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        return []

    def fetch_holdings(self) -> List[FundHoldings]:
        return fetch_etf_top_holdings(
            ticker="MOAT",
            fund_name="VanEck Morningstar Wide Moat ETF",
            provider_name=self.provider_name,
        )
