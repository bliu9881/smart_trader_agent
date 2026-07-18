"""
SEC EDGAR Provider — insider trading filings (Form 4).

Fetches recent Form 4 filings from the SEC EDGAR full-text search API,
then parses the ownership XML documents to extract insider transactions.
This is the authoritative, free, always-available source for insider trades.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests

from smart_trader.core.smart_money import DataProvider, TradeFiling
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)

# SEC requires: "Company Name AdminContact@Company.com"
_USER_AGENT = "RegimeTrader admin@regimetrader.com"

# Corporate titles / abbreviations that look like tickers but aren't
_FALSE_TICKER_BLOCKLIST = frozenset({
    "CEO", "CFO", "COO", "CTO", "CIO", "CMO", "CLO", "CSO",
    "VP", "SVP", "EVP", "AVP",
    "DIR", "MD", "GC", "CAO", "CCO", "CDO", "CHRO", "CRO",
    "SEC", "TRES", "CTR",
    "NA", "NAN", "NULL", "NONE", "TBD", "USD", "INC", "LLC", "LTD", "ETF",
})

# EDGAR full-text search API
_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# Transaction codes that represent open-market purchases
_BUY_CODES = {"P"}  # P = open-market purchase
# Transaction codes that represent dispositions/sales
_SELL_CODES = {"S"}  # S = open-market sale
# A = grant/award (not open-market, skip for smart money signal)
# Other codes: M (exercise), G (gift), etc. — not market signals


class SECEdgarProvider(DataProvider):
    """Fetches SEC Form 4 insider trading filings from EDGAR."""

    def __init__(self, config: SmartMoneyConfig):
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })

    @property
    def provider_name(self) -> str:
        return "sec_edgar"

    def get_cache_ttl_hours(self) -> float:
        return self.config.sec_edgar_cache_ttl

    def fetch_raw_data(self) -> str:
        """Fetch recent Form 4 filing index from EDGAR EFTS API.

        Returns JSON string of the search results. We then parse individual
        Form 4 XML documents in parse_filings().
        """
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.config.recency_window_days)

            params = {
                "forms": "4",
                "startdt": start_date.strftime("%Y-%m-%d"),
                "enddt": end_date.strftime("%Y-%m-%d"),
                "from": 0,
                "size": 100,  # Top 100 filings
            }
            resp = self._session.get(_EFTS_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.error(f"SEC EDGAR fetch failed: {e}")
            return ""

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        """Parse EDGAR search results, fetch individual Form 4 XMLs, extract transactions."""
        if not raw_data:
            return []

        import json
        try:
            data = json.loads(raw_data)
        except Exception as e:
            logger.warning(f"SEC EDGAR: failed to parse search JSON: {e}")
            return []

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            logger.info("SEC EDGAR: no Form 4 filings found in search results")
            return []

        filings: List[TradeFiling] = []
        fetched = 0
        errors = 0

        for hit in hits:
            source = hit.get("_source", {})
            doc_id = hit.get("_id", "")

            # Extract metadata from search index
            adsh = source.get("adsh", "")
            ciks = source.get("ciks", [])
            display_names = source.get("display_names", [])
            file_date = source.get("file_date", "")

            if not adsh or len(ciks) < 2:
                continue

            # First CIK is typically the insider, second is the issuer
            issuer_cik = ciks[1].lstrip("0") if len(ciks) >= 2 else ""

            # Extract XML filename from doc_id (format: "adsh:filename.xml")
            xml_filename = ""
            if ":" in doc_id:
                xml_filename = doc_id.split(":", 1)[1]

            if not issuer_cik or not xml_filename:
                continue

            # Fetch and parse the Form 4 XML
            try:
                txns = self._fetch_form4_xml(issuer_cik, adsh, xml_filename, file_date)
                filings.extend(txns)
                fetched += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    logger.debug(f"SEC EDGAR: failed to parse {adsh}: {e}")

            # Rate limit: SEC asks for max 10 requests/sec
            if fetched % 10 == 0 and fetched > 0:
                import time
                time.sleep(1.1)

        logger.info(
            f"SEC EDGAR: fetched {fetched} Form 4 documents, "
            f"parsed {len(filings)} transactions ({errors} errors)"
        )
        return filings

    def _fetch_form4_xml(
        self,
        issuer_cik: str,
        adsh: str,
        xml_filename: str,
        file_date: str,
    ) -> List[TradeFiling]:
        """Fetch and parse a single Form 4 XML document."""
        # EDGAR archive URL format
        adsh_nodash = adsh.replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{issuer_cik}/{adsh_nodash}/{xml_filename}"
        )

        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()

        return _parse_ownership_xml(resp.text, file_date)


def _parse_ownership_xml(xml_text: str, file_date_str: str) -> List[TradeFiling]:
    """Parse an ownershipDocument XML into TradeFiling records."""
    filings: List[TradeFiling] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug(f"SEC EDGAR: XML parse error: {e}")
        return []

    # Extract issuer info
    issuer = root.find("issuer")
    if issuer is None:
        return []

    ticker = _xml_text(issuer, "issuerTradingSymbol", "").upper().strip()
    if not ticker or not ticker.isalpha() or len(ticker) > 6:
        return []

    # Reject corporate titles that sometimes appear as trading symbols
    if ticker in _FALSE_TICKER_BLOCKLIST:
        return []

    # Extract insider info
    owner = root.find("reportingOwner")
    if owner is None:
        return []

    owner_name = _xml_text(
        owner, "reportingOwnerId/rptOwnerName", ""
    ).strip()
    if not owner_name:
        return []

    # Extract title/relationship
    rel = owner.find("reportingOwnerRelationship")
    title = ""
    if rel is not None:
        title = _xml_text(rel, "officerTitle", "").strip()
        if not title:
            if _xml_text(rel, "isDirector", "0") == "1":
                title = "Director"
            elif _xml_text(rel, "isTenPercentOwner", "0") == "1":
                title = "10% Owner"

    actor = f"{owner_name} ({title})" if title else owner_name

    # Parse filing date
    filing_date = _parse_date(file_date_str)
    if filing_date is None:
        filing_date = datetime.now()

    # Parse non-derivative transactions
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for txn in nd_table.findall("nonDerivativeTransaction"):
            filing = _parse_transaction(txn, ticker, actor, filing_date)
            if filing is not None:
                filings.append(filing)

    return filings


def _parse_transaction(
    txn: ET.Element,
    ticker: str,
    actor: str,
    filing_date: datetime,
) -> Optional[TradeFiling]:
    """Parse a single nonDerivativeTransaction element."""
    # Transaction code
    coding = txn.find("transactionCoding")
    if coding is None:
        return None

    tx_code = _xml_text(coding, "transactionCode", "")
    if tx_code not in _BUY_CODES | _SELL_CODES:
        return None

    # Transaction date
    trade_date_str = _xml_text(txn, "transactionDate/value", "")
    trade_date = _parse_date(trade_date_str)
    if trade_date is None:
        return None

    # Shares and price
    amounts = txn.find("transactionAmounts")
    if amounts is None:
        return None

    shares_str = _xml_text(amounts, "transactionShares/value", "0")
    price_str = _xml_text(amounts, "transactionPricePerShare/value", "0")
    acq_disp = _xml_text(amounts, "transactionAcquiredDisposedCode/value", "")

    try:
        shares = float(shares_str)
        price = float(price_str) if price_str else 0.0
    except (ValueError, TypeError):
        return None

    if shares <= 0:
        return None

    dollar_amount = shares * price if price > 0 else None
    share_change = int(shares)

    # Determine transaction type
    if tx_code in _BUY_CODES or acq_disp == "A":
        tx_type = "buy"
    else:
        tx_type = "sell"

    return TradeFiling(
        source="sec_edgar",
        actor=actor,
        symbol=ticker,
        tx_type=tx_type,
        dollar_amount=dollar_amount,
        share_change=share_change,
        filing_date=filing_date,
        trade_date=trade_date,
    )


def _xml_text(parent: ET.Element, path: str, default: str = "") -> str:
    """Safely extract text from an XML element by path."""
    el = parent.find(path)
    if el is not None and el.text:
        return el.text.strip()
    return default


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse common date formats from EDGAR."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None
