"""Shared SEC EDGAR 13F fetch/parse utilities.

Used by the four 13F-based Holdings_Providers: Berkshire, Pershing Square,
Appaloosa, and Duquesne. Encapsulates:
  - The submissions JSON → latest-13F-filing lookup
  - The information-table XML fetch (trying a few filename variants EDGAR uses)
  - The XML → FundHoldings conversion, with name→ticker best-effort resolution
  - Per-position weight computation (market_value / sum(market_value))

The delta-style (TradeFiling) parsing in berkshire_13f.py is NOT touched — this
module is additive, only used by fetch_holdings().
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

import requests

from smart_trader.core.smart_money import FundHoldings

logger = logging.getLogger(__name__)

_SEC_HEADERS = {
    "User-Agent": "SmartTrader/1.0 (research@example.com)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Name → ticker mapping
# ---------------------------------------------------------------------------
# Covers the frequent holdings of Berkshire, Pershing Square, Appaloosa, and
# Duquesne. Unknown names are logged at debug and skipped — the universe
# size is robust to a few missing tickers, and the user can extend this map
# when inspecting cached raw data.

_NAME_TO_TICKER: Dict[str, str] = {
    # Berkshire staples
    "APPLE": "AAPL",
    "BANK OF AMERICA": "BAC",
    "AMERICAN EXPRESS": "AXP",
    "COCA-COLA": "KO",
    "COCA COLA": "KO",
    "CHEVRON": "CVX",
    "OCCIDENTAL": "OXY",
    "KRAFT HEINZ": "KHC",
    "MOODY": "MCO",
    "MOODYS": "MCO",
    "DAVITA": "DVA",
    "VERISIGN": "VRSN",
    "LIBERTY MEDIA": "LSXMA",
    "KROGER": "KR",
    "SIRIUS": "SIRI",
    "VISA": "V",
    "MASTERCARD": "MA",
    "AMAZON": "AMZN",
    "SNOWFLAKE": "SNOW",
    "NU HOLDINGS": "NU",
    "CITIGROUP": "C",
    "CAPITAL ONE": "COF",
    "T-MOBILE": "TMUS",
    "CHARTER COMM": "CHTR",
    "PARAMOUNT": "PARA",
    "AON": "AON",
    "HP": "HPQ",
    "ALLY FINANCIAL": "ALLY",
    "MARKEL": "MKL",
    "FLOOR & DECOR": "FND",
    "LOUISIANA-PACIFIC": "LPX",
    "LENNAR": "LEN",
    "NVR": "NVR",
    "DOMINOS": "DPZ",
    "DOMINO": "DPZ",
    "POOL": "POOL",
    "CONSTELLATION BRANDS": "STZ",
    "DIAGEO": "DEO",
    "HEICO": "HEI",
    # Pershing Square common holdings
    "CHIPOTLE": "CMG",
    "HILTON": "HLT",
    "CANADIAN PACIFIC": "CP",
    "RESTAURANT BRANDS": "QSR",
    "UNIVERSAL MUSIC": "UMGNF",
    "HOWARD HUGHES": "HHH",
    "LOWES": "LOW",
    "LOWE'S": "LOW",
    "ALPHABET": "GOOGL",
    "GOOGLE": "GOOGL",
    "FANNIE MAE": "FNMA",
    "FREDDIE MAC": "FMCC",
    "AIR PRODUCTS": "APD",
    "BROOKFIELD": "BN",
    # Appaloosa / Tepper common holdings
    "META PLATFORMS": "META",
    "META": "META",
    "FACEBOOK": "META",
    "MICRON": "MU",
    "ADVANCED MICRO": "AMD",
    "NVIDIA": "NVDA",
    "UBER": "UBER",
    "ENERGY TRANSFER": "ET",
    "ANTERO": "AR",
    "CAESARS": "CZR",
    "UNITED AIRLINES": "UAL",
    "DELTA AIR": "DAL",
    "AMERICAN AIRLINES": "AAL",
    "ALIBABA": "BABA",
    "PDD HOLDINGS": "PDD",
    "JD.COM": "JD",
    "KE HOLDINGS": "BEKE",
    "BAIDU": "BIDU",
    # Duquesne / Druckenmiller common holdings
    "MICROSOFT": "MSFT",
    "COHERENT": "COHR",
    "COUPANG": "CPNG",
    "EATON": "ETN",
    "NATERA": "NTRA",
    "TAIWAN SEMICONDUCTOR": "TSM",
    "TSMC": "TSM",
    "WOODWARD": "WWD",
    "MARVELL": "MRVL",
    "SERVICENOW": "NOW",
    "PALANTIR": "PLTR",
    # Shared megacap / common long positions
    "SALESFORCE": "CRM",
    "ORACLE": "ORCL",
    "BROADCOM": "AVGO",
    "INTEL": "INTC",
    "QUALCOMM": "QCOM",
    "ADOBE": "ADBE",
    "NETFLIX": "NFLX",
    "WALT DISNEY": "DIS",
    "DISNEY": "DIS",
    "JOHNSON & JOHNSON": "JNJ",
    "JOHNSON AND JOHNSON": "JNJ",
    "PFIZER": "PFE",
    "ELI LILLY": "LLY",
    "LILLY": "LLY",
    "MERCK": "MRK",
    "ABBVIE": "ABBV",
    "UNITEDHEALTH": "UNH",
    "BERKSHIRE HATHAWAY": "BRK.B",
    "GOLDMAN SACHS": "GS",
    "MORGAN STANLEY": "MS",
    "BLACKROCK": "BLK",
    "S&P GLOBAL": "SPGI",
    "INTERCONTINENTAL EXCHANGE": "ICE",
    "CME GROUP": "CME",
    "PROCTER & GAMBLE": "PG",
    "PROCTER AND GAMBLE": "PG",
    "WALMART": "WMT",
    "COSTCO": "COST",
    "HOME DEPOT": "HD",
    "TESLA": "TSLA",
    "GENERAL MOTORS": "GM",
    "FORD MOTOR": "F",
    "EXXON": "XOM",
    "EXXONMOBIL": "XOM",
    "CATERPILLAR": "CAT",
    "GENERAL ELECTRIC": "GE",
    "BOEING": "BA",
    "RAYTHEON": "RTX",
    "LOCKHEED": "LMT",
}


def _name_to_ticker(name: str) -> str:
    """Best-effort mapping from 13F nameOfIssuer to ticker. '' on miss."""
    if not name:
        return ""
    name_upper = name.upper()
    for key, ticker in _NAME_TO_TICKER.items():
        if key in name_upper:
            return ticker
    return ""


# ---------------------------------------------------------------------------
# Filing discovery
# ---------------------------------------------------------------------------

def get_latest_13f_filing(cik: str) -> Optional[Tuple[str, datetime]]:
    """Return (accession_number, filing_date) for the most recent 13F-HR.

    Returns None if no 13F filing is found or the request fails. Accession
    number is returned in the dash-free form suitable for URL construction.
    """
    recent = get_recent_13f_filings(cik, n=1)
    return recent[0] if recent else None


def get_recent_13f_filings(cik: str, n: int = 2) -> List[Tuple[str, datetime]]:
    """Return up to `n` most recent 13F-HR filings as (accession, filing_date).

    Accession numbers are returned dash-free for URL construction. Returns an
    empty list on fetch failure or when no 13F filings exist for the CIK.
    """
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"EDGAR submissions fetch failed for CIK {cik}: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {}) or data.get("recent", {})
    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])

    out: List[Tuple[str, datetime]] = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            try:
                accession = accession_numbers[i].replace("-", "")
                filing_date = datetime.strptime(filing_dates[i], "%Y-%m-%d")
                out.append((accession, filing_date))
            except Exception:
                continue
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------------------
# Information-table fetch (tries a few filename variants)
# ---------------------------------------------------------------------------

def _fetch_info_table(cik: str, accession: str) -> Optional[str]:
    """Fetch the 13F information-table XML. Returns None on failure.

    EDGAR names the info-table XML differently per filing (sometimes a numeric
    stem, sometimes `informationtable.xml`, sometimes with the accession
    prefix). We list the filing directory via index.json and test each .xml
    file for an infoTable tag.
    """
    cik_no_leading_zeros = str(int(cik))
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_no_leading_zeros}/{accession}"

    # Discover filenames via the filing index
    try:
        idx_resp = requests.get(f"{base}/index.json", headers=_SEC_HEADERS, timeout=30)
        idx_resp.raise_for_status()
        items = idx_resp.json().get("directory", {}).get("item", [])
    except Exception as e:
        logger.warning(f"EDGAR index.json fetch failed for CIK {cik} acc {accession}: {e}")
        items = []

    xml_names = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]
    # primary_doc.xml is the cover page — try non-primary files first
    xml_names.sort(key=lambda n: (n == "primary_doc.xml", n))

    for name in xml_names:
        try:
            resp = requests.get(f"{base}/{name}", headers=_SEC_HEADERS, timeout=30)
            if resp.status_code == 200 and "infoTable" in resp.text:
                return resp.text
        except Exception:
            continue

    # Legacy fallback for older filings not present in index.json
    for name in ("infotable.xml", "informationtable.xml"):
        try:
            resp = requests.get(f"{base}/{name}", headers=_SEC_HEADERS, timeout=30)
            if resp.status_code == 200 and "infoTable" in resp.text:
                return resp.text
        except Exception:
            continue

    logger.warning(f"EDGAR 13F info-table fetch failed for CIK {cik} acc {accession}")
    return None


# ---------------------------------------------------------------------------
# XML → holdings
# ---------------------------------------------------------------------------

def _parse_info_table_xml(xml_text: str) -> List[Dict]:
    """Parse 13F information-table XML to [{name, shares, value, cusip}]."""
    rows: List[Dict] = []
    try:
        # Strip (a) xmlns declarations, (b) namespace prefixes on tag names,
        # (c) namespace prefixes on attribute names (`xsi:schemaLocation` etc.)
        # so ElementTree doesn't choke on unbound prefixes and we can match
        # tag names directly.
        cleaned = re.sub(r'\sxmlns(:[\w.-]+)?="[^"]*"', "", xml_text)
        cleaned = re.sub(r'<(/?)[\w.-]+:', r'<\1', cleaned)
        cleaned = re.sub(r'\s[\w.-]+:([\w.-]+\s*=)', r' \1', cleaned)
        root = ElementTree.fromstring(cleaned)
    except Exception as e:
        logger.warning(f"13F XML parse error: {e}")
        return []

    for info_table in root.iter():
        if "infoTable" not in info_table.tag:
            continue
        name = ""
        cusip = ""
        value_raw = 0
        shares = 0
        for child in info_table:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "nameOfIssuer":
                name = (child.text or "").strip()
            elif tag == "cusip":
                cusip = (child.text or "").strip()
            elif tag == "value":
                try:
                    value_raw = int((child.text or "0").strip())
                except ValueError:
                    value_raw = 0
            elif tag == "shrsOrPrnAmt":
                for sub in child:
                    sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    if sub_tag == "sshPrnamt":
                        try:
                            shares = int((sub.text or "0").strip())
                        except ValueError:
                            shares = 0
        if shares > 0 and value_raw > 0:
            rows.append({
                "name": name,
                "cusip": cusip,
                "shares": shares,
                "value_raw": value_raw,
            })

    # SEC changed 13F `value` semantics in 2022/2023: pre-change filings
    # report thousands of dollars, newer filings report dollars. A single
    # position rarely exceeds $1B even at the largest funds, so if the
    # maximum raw value is > 1e9, the filing is already in dollars.
    if not rows:
        return rows
    max_raw = max(r["value_raw"] for r in rows)
    multiplier = 1.0 if max_raw > 1e9 else 1000.0
    for r in rows:
        r["value_usd"] = float(r["value_raw"]) * multiplier
        del r["value_raw"]
    return rows


def fetch_latest_13f_holdings(
    cik: str,
    fund_name: str,
    provider_name: str,
) -> List[FundHoldings]:
    """Fetch and parse the most recent 13F-HR for `cik`. Returns FundHoldings."""
    latest = get_latest_13f_filing(cik)
    if latest is None:
        return []

    accession, filing_date = latest
    xml_text = _fetch_info_table(cik, accession)
    if xml_text is None:
        return []

    rows = _parse_info_table_xml(xml_text)
    if not rows:
        return []

    # Aggregate duplicates (different classes / CUSIPs mapping to same ticker)
    total_value = sum(r["value_usd"] for r in rows)
    if total_value <= 0:
        return []

    agg: Dict[str, Dict] = {}
    unmapped: int = 0
    for r in rows:
        ticker = _name_to_ticker(r["name"])
        if not ticker:
            unmapped += 1
            logger.debug(f"{provider_name}: unmapped 13F name '{r['name']}' (CUSIP {r['cusip']})")
            continue
        if ticker not in agg:
            agg[ticker] = {"shares": 0, "value_usd": 0.0}
        agg[ticker]["shares"] += r["shares"]
        agg[ticker]["value_usd"] += r["value_usd"]

    holdings: List[FundHoldings] = [
        FundHoldings(
            fund_name=fund_name,
            provider_name=provider_name,
            symbol=ticker,
            share_count=data["shares"],
            holding_weight=data["value_usd"] / total_value,
            market_value=data["value_usd"],
            as_of_date=filing_date,
        )
        for ticker, data in agg.items()
    ]
    logger.info(
        f"{provider_name}: fetched {len(holdings)} holdings from 13F "
        f"(filing {filing_date.date()}, {unmapped} unmapped names skipped)"
    )
    return holdings
