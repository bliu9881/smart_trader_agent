"""
Berkshire 13F Provider — Warren Buffett's quarterly holdings.

Compares the two most recent Berkshire Hathaway 13F-HR filings to emit
TradeFiling deltas (new positions and increases). The information-table XML
is discovered via EDGAR's index.json — the file is named differently per
filing (often a numeric stem like 50240.xml), so a hardcoded filename list
breaks regularly. Holdings-snapshot fetching for Smart_Money_Portfolio is
delegated to fetch_latest_13f_holdings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List

from smart_trader.core.smart_money import DataProvider, FundHoldings, TradeFiling
from smart_trader.core.smart_money_providers._edgar_13f import (
    _fetch_info_table,
    _name_to_ticker,
    _parse_info_table_xml,
    fetch_latest_13f_holdings,
    get_recent_13f_filings,
)
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)

_BERKSHIRE_CIK = "0001067983"


class BerkshireProvider(DataProvider):
    """Fetches Berkshire Hathaway 13F filings from SEC EDGAR."""

    def __init__(self, config: SmartMoneyConfig):
        self.config = config

    @property
    def provider_name(self) -> str:
        return "berkshire_13f"

    def get_cache_ttl_hours(self) -> float:
        return self.config.berkshire_cache_ttl

    def refresh_mode(self) -> str:
        return "filing_detect"

    def fetch_holdings(self) -> List[FundHoldings]:
        return fetch_latest_13f_holdings(
            cik=_BERKSHIRE_CIK,
            fund_name="Berkshire Hathaway",
            provider_name=self.provider_name,
        )

    def fetch_raw_data(self) -> str:
        # The DataProvider contract expects a string; the rich filing discovery
        # happens inside parse_filings via the EDGAR helpers, so this just
        # signals a successful "fetch" so parse_filings runs.
        return "ok"

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        if not raw_data:
            return []

        recent = get_recent_13f_filings(_BERKSHIRE_CIK, n=2)
        if not recent:
            logger.info("Berkshire 13F: no 13F-HR filings found")
            return []

        cutoff = datetime.now() - timedelta(days=self.config.berkshire_recency_days)
        latest_acc, latest_date = recent[0]
        if latest_date < cutoff:
            logger.info(
                f"Berkshire 13F: latest filing {latest_date.date()} older than recency window"
            )
            return []

        current = _holdings_by_ticker(latest_acc)
        if not current:
            logger.info("Berkshire 13F: could not parse current holdings")
            return []

        prior: Dict[str, int] = {}
        if len(recent) >= 2:
            prior = _holdings_by_ticker(recent[1][0])

        filings: List[TradeFiling] = []
        for symbol, current_shares in current.items():
            prior_shares = prior.get(symbol, 0)
            if current_shares > prior_shares:
                tx_type = "buy" if prior_shares == 0 else "increase"
                filings.append(TradeFiling(
                    source="berkshire_13f",
                    actor="Berkshire Hathaway",
                    symbol=symbol,
                    tx_type=tx_type,
                    dollar_amount=None,
                    share_change=current_shares - prior_shares,
                    filing_date=latest_date,
                    trade_date=latest_date,
                ))

        logger.info(f"Berkshire 13F: parsed {len(filings)} position changes")
        return filings


def _holdings_by_ticker(accession: str) -> Dict[str, int]:
    """Fetch and parse the 13F info-table for an accession; aggregate by ticker."""
    xml = _fetch_info_table(_BERKSHIRE_CIK, accession)
    if xml is None:
        return {}
    rows = _parse_info_table_xml(xml)
    out: Dict[str, int] = {}
    for r in rows:
        ticker = _name_to_ticker(r["name"])
        if ticker:
            out[ticker] = out.get(ticker, 0) + int(r["shares"])
    return out
