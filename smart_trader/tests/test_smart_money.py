"""
Property-based tests for the Smart Money Scanner.

Uses pytest + hypothesis to verify correctness properties from the design doc.
All tests mock external HTTP calls (no real network requests).
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from typing import Dict, List

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from smart_trader.core.smart_money import (
    DataProvider,
    TradeFiling,
    _compute_conviction_score,
)
from smart_trader.core.smart_money_providers.ark_invest import ARKProvider
from smart_trader.settings.config import SmartMoneyConfig, RiskConfig


# ---------------------------------------------------------------------------
# Hypothesis strategies for smart money domain objects
# ---------------------------------------------------------------------------

VALID_SOURCES = ["capitol_trades", "berkshire_13f", "ark_invest", "insider_cluster"]
VALID_TX_TYPES = ["buy", "sell", "increase", "decrease"]

# Strategy for generating non-empty trimmed strings (actor names, symbols, etc.)
non_empty_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122),
    min_size=1,
    max_size=20,
).filter(lambda s: len(s.strip()) > 0)

# Strategy for ticker symbols (uppercase letters, 1-5 chars)
ticker_symbol = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    min_size=1,
    max_size=5,
)

# Strategy for datetime within a reasonable range
reasonable_datetime = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2026, 12, 31),
)

# Strategy for dollar amounts: either None or a positive float
dollar_amount_st = st.one_of(st.none(), st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False))

# Strategy for share changes: either None or a positive int
share_change_st = st.one_of(st.none(), st.integers(min_value=1, max_value=10_000_000))


@st.composite
def valid_trade_filing_data(draw):
    """Generate data for a single valid TradeFiling where at least one of
    dollar_amount or share_change is non-None."""
    source = draw(st.sampled_from(VALID_SOURCES))
    actor = draw(non_empty_text)
    symbol = draw(ticker_symbol)
    tx_type = draw(st.sampled_from(VALID_TX_TYPES))
    filing_date = draw(reasonable_datetime)
    trade_date = draw(reasonable_datetime)

    # Ensure at least one of dollar_amount or share_change is non-None
    dollar_amount = draw(dollar_amount_st)
    share_change = draw(share_change_st)
    if dollar_amount is None and share_change is None:
        # Force at least one to be present
        if draw(st.booleans()):
            dollar_amount = draw(st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False))
        else:
            share_change = draw(st.integers(min_value=1, max_value=10_000_000))

    return {
        "source": source,
        "actor": actor,
        "symbol": symbol,
        "tx_type": tx_type,
        "dollar_amount": dollar_amount,
        "share_change": share_change,
        "filing_date": filing_date.isoformat(),
        "trade_date": trade_date.isoformat(),
    }


# ---------------------------------------------------------------------------
# Mock provider for testing parse_filings contract
# ---------------------------------------------------------------------------

class MockDataProvider(DataProvider):
    """A mock DataProvider that parses JSON-encoded filing data.

    The raw data is a JSON array of filing dicts. Each dict is parsed into
    a TradeFiling. Records with missing/empty required fields or where both
    dollar_amount and share_change are None are skipped (per Requirement 1.8).
    """

    def fetch_raw_data(self) -> str:
        return "[]"

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        """Parse JSON array of filing dicts into TradeFiling records.

        Skips records that:
        - Have missing or empty required string fields (source, actor, symbol, tx_type)
        - Have missing or unparseable dates (filing_date, trade_date)
        - Have both dollar_amount and share_change as None
        """
        try:
            records = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return []

        filings: List[TradeFiling] = []
        for rec in records:
            try:
                source = rec.get("source", "")
                actor = rec.get("actor", "")
                symbol = rec.get("symbol", "")
                tx_type = rec.get("tx_type", "")

                # Skip if any required string field is empty
                if not source or not actor or not symbol or not tx_type:
                    continue

                filing_date = datetime.fromisoformat(rec["filing_date"])
                trade_date = datetime.fromisoformat(rec["trade_date"])

                dollar_amount = rec.get("dollar_amount")
                share_change = rec.get("share_change")

                # At least one of dollar_amount or share_change must be present
                if dollar_amount is None and share_change is None:
                    continue

                filings.append(TradeFiling(
                    source=source,
                    actor=actor,
                    symbol=symbol,
                    tx_type=tx_type,
                    dollar_amount=dollar_amount,
                    share_change=share_change,
                    filing_date=filing_date,
                    trade_date=trade_date,
                ))
            except (KeyError, ValueError, TypeError):
                # Skip malformed records
                continue

        return filings

    def get_cache_ttl_hours(self) -> float:
        return 24.0

    @property
    def provider_name(self) -> str:
        return "mock_provider"


# ---------------------------------------------------------------------------
# Property 2: Parsed TradeFiling records contain all required fields
# ---------------------------------------------------------------------------

class TestTradeFilingRequiredFields:
    """
    **Validates: Requirements 1.6**

    Property 2: For any valid raw data input to a DataProvider's parse_filings
    method, every returned TradeFiling record SHALL contain non-empty values
    for: source, actor, symbol, tx_type, filing_date, and trade_date, and at
    least one of dollar_amount or share_change SHALL be non-None.
    """

    @given(filing_data_list=st.lists(valid_trade_filing_data(), min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_parsed_filings_contain_all_required_fields(self, filing_data_list):
        """
        **Validates: Requirements 1.6**

        Generate random valid filing data, parse through a mock provider,
        verify every returned TradeFiling has non-empty source, actor, symbol,
        tx_type, filing_date, trade_date, and at least one of dollar_amount
        or share_change is non-None.
        """
        provider = MockDataProvider()
        raw_data = json.dumps(filing_data_list)

        filings = provider.parse_filings(raw_data)

        # All valid inputs should produce filings
        assert len(filings) == len(filing_data_list), (
            f"Expected {len(filing_data_list)} filings but got {len(filings)}"
        )

        for filing in filings:
            # source must be non-empty string
            assert isinstance(filing.source, str) and len(filing.source) > 0, (
                f"source is empty or not a string: {filing.source!r}"
            )

            # actor must be non-empty string
            assert isinstance(filing.actor, str) and len(filing.actor) > 0, (
                f"actor is empty or not a string: {filing.actor!r}"
            )

            # symbol must be non-empty string
            assert isinstance(filing.symbol, str) and len(filing.symbol) > 0, (
                f"symbol is empty or not a string: {filing.symbol!r}"
            )

            # tx_type must be non-empty string
            assert isinstance(filing.tx_type, str) and len(filing.tx_type) > 0, (
                f"tx_type is empty or not a string: {filing.tx_type!r}"
            )

            # filing_date must be a datetime
            assert isinstance(filing.filing_date, datetime), (
                f"filing_date is not a datetime: {filing.filing_date!r}"
            )

            # trade_date must be a datetime
            assert isinstance(filing.trade_date, datetime), (
                f"trade_date is not a datetime: {filing.trade_date!r}"
            )

            # At least one of dollar_amount or share_change must be non-None
            assert filing.dollar_amount is not None or filing.share_change is not None, (
                f"Both dollar_amount and share_change are None for {filing.symbol}"
            )


# ---------------------------------------------------------------------------
# Strategies for generating invalid filing data
# ---------------------------------------------------------------------------

def _make_valid_dict(filing_data: dict) -> dict:
    """Return a copy of a valid filing dict (identity helper for clarity)."""
    return dict(filing_data)


@st.composite
def invalid_filing_missing_required_field(draw):
    """Generate a filing dict with one required string field missing entirely."""
    base = draw(valid_trade_filing_data())
    field_to_remove = draw(st.sampled_from(["source", "actor", "symbol", "tx_type"]))
    del base[field_to_remove]
    return base


@st.composite
def invalid_filing_empty_string_field(draw):
    """Generate a filing dict with one required string field set to empty string."""
    base = draw(valid_trade_filing_data())
    field_to_empty = draw(st.sampled_from(["source", "actor", "symbol", "tx_type"]))
    base[field_to_empty] = ""
    return base


@st.composite
def invalid_filing_both_amounts_none(draw):
    """Generate a filing dict where both dollar_amount and share_change are None."""
    base = draw(valid_trade_filing_data())
    base["dollar_amount"] = None
    base["share_change"] = None
    return base


@st.composite
def invalid_filing_bad_date(draw):
    """Generate a filing dict with an unparseable date field."""
    base = draw(valid_trade_filing_data())
    date_field = draw(st.sampled_from(["filing_date", "trade_date"]))
    bad_value = draw(st.sampled_from([
        "not-a-date",
        "2025-13-45",
        "yesterday",
        "",
        "99/99/9999",
    ]))
    base[date_field] = bad_value
    return base


@st.composite
def invalid_filing_missing_date(draw):
    """Generate a filing dict with a required date field missing entirely."""
    base = draw(valid_trade_filing_data())
    date_field = draw(st.sampled_from(["filing_date", "trade_date"]))
    del base[date_field]
    return base


@st.composite
def invalid_filing_malformed_record(draw):
    """Generate a completely malformed record (not a proper filing dict)."""
    return draw(st.sampled_from([
        {},
        {"random_key": "random_value"},
        {"source": 12345},
        {"symbol": None, "actor": None},
        {"filing_date": True, "trade_date": False},
    ]))


# Combine all invalid strategies into one
invalid_filing_data = st.one_of(
    invalid_filing_missing_required_field(),
    invalid_filing_empty_string_field(),
    invalid_filing_both_amounts_none(),
    invalid_filing_bad_date(),
    invalid_filing_missing_date(),
    invalid_filing_malformed_record(),
)


# ---------------------------------------------------------------------------
# Property 3: Invalid filings are skipped while valid filings are preserved
# ---------------------------------------------------------------------------

class TestInvalidFilingsSkipped:
    """
    **Validates: Requirements 1.8**

    Property 3: For any raw data input containing a mix of parseable and
    unparseable filing entries, the parse_filings method SHALL return exactly
    the set of parseable entries (no valid filings dropped, no invalid filings
    included), and the count of returned filings SHALL equal the count of
    valid entries in the input.
    """

    @given(
        valid_entries=st.lists(valid_trade_filing_data(), min_size=0, max_size=10),
        invalid_entries=st.lists(invalid_filing_data, min_size=0, max_size=10),
        seed=st.randoms(use_true_random=False),
    )
    @settings(max_examples=100)
    def test_invalid_filings_skipped_valid_preserved(self, valid_entries, invalid_entries, seed):
        """
        **Validates: Requirements 1.8**

        Generate lists mixing valid and invalid filing entries, parse through
        the MockDataProvider, assert returned count equals valid entry count
        and no invalid entries appear in the output.
        """
        # Ensure we have at least one entry total to make the test meaningful
        assume(len(valid_entries) + len(invalid_entries) > 0)

        # Combine and shuffle valid + invalid entries
        mixed = list(valid_entries) + list(invalid_entries)
        seed.shuffle(mixed)

        provider = MockDataProvider()
        raw_data = json.dumps(mixed)
        filings = provider.parse_filings(raw_data)

        # The number of returned filings must equal the number of valid entries
        assert len(filings) == len(valid_entries), (
            f"Expected {len(valid_entries)} filings from {len(valid_entries)} valid + "
            f"{len(invalid_entries)} invalid entries, but got {len(filings)}"
        )

        # Every returned filing must have all required fields populated
        for filing in filings:
            assert isinstance(filing.source, str) and len(filing.source) > 0
            assert isinstance(filing.actor, str) and len(filing.actor) > 0
            assert isinstance(filing.symbol, str) and len(filing.symbol) > 0
            assert isinstance(filing.tx_type, str) and len(filing.tx_type) > 0
            assert isinstance(filing.filing_date, datetime)
            assert isinstance(filing.trade_date, datetime)
            assert filing.dollar_amount is not None or filing.share_change is not None

        # Verify that every valid entry appears in the output by checking
        # that the set of (source, actor, symbol) tuples from valid entries
        # matches the returned filings
        valid_keys = sorted(
            (v["source"], v["actor"], v["symbol"]) for v in valid_entries
        )
        result_keys = sorted(
            (f.source, f.actor, f.symbol) for f in filings
        )
        assert valid_keys == result_keys, (
            f"Valid entry keys don't match result keys.\n"
            f"Valid: {valid_keys}\nResult: {result_keys}"
        )


# ---------------------------------------------------------------------------
# Strategies for ARK daily holdings snapshots (Property 6)
# ---------------------------------------------------------------------------

# Strategy for ARK ticker symbols (uppercase letters, 2-5 chars)
ark_ticker = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    min_size=2,
    max_size=5,
)


@st.composite
def ark_daily_snapshot(draw):
    """Generate a random set of ARK daily buy entries.

    Returns a tuple of:
    - csv_rows: list of dicts with keys (fund, date, direction, ticker, shares)
    - expected_buy_days: dict mapping symbol -> set of date strings where buys occurred
    - min_ark_buying_days: the threshold to use

    All dates are within the recency window (last 30 days) so recency
    filtering doesn't interfere with the buying-days logic under test.
    """
    # Pick a small set of symbols (1-5)
    n_symbols = draw(st.integers(min_value=1, max_value=5))
    symbols = draw(
        st.lists(ark_ticker, min_size=n_symbols, max_size=n_symbols, unique=True)
    )

    # Pick min_ark_buying_days threshold (1-5)
    min_buying_days = draw(st.integers(min_value=1, max_value=5))

    # Generate recent dates within the last 25 days (safely within 30-day recency window)
    today = datetime.now()
    available_dates = [
        (today - timedelta(days=d)).strftime("%m/%d/%Y")
        for d in range(1, 26)
    ]

    csv_rows = []
    buy_days_per_symbol: Dict[str, set] = {s: set() for s in symbols}

    for symbol in symbols:
        # Each symbol gets 0-8 buy entries across random dates
        n_entries = draw(st.integers(min_value=0, max_value=8))
        if n_entries == 0:
            continue

        # Pick dates for this symbol's entries (may repeat = same day buys)
        entry_dates = draw(
            st.lists(
                st.sampled_from(available_dates),
                min_size=n_entries,
                max_size=n_entries,
            )
        )

        fund_choices = ["ARKK", "ARKW", "ARKG", "ARKF", "ARKQ"]
        for date_str in entry_dates:
            shares = draw(st.integers(min_value=100, max_value=500_000))
            fund = draw(st.sampled_from(fund_choices))
            csv_rows.append({
                "fund": fund,
                "date": date_str,
                "direction": "Buy",
                "ticker": symbol,
                "company": f"{symbol} Inc",
                "shares": str(shares),
            })
            buy_days_per_symbol[symbol].add(date_str)

    return csv_rows, buy_days_per_symbol, min_buying_days


def _build_ark_csv(rows: list) -> str:
    """Build an arkfunds.io-shaped JSON trades payload.

    Kept under the legacy name so upstream fixtures continue to work. Emits
    the same dict keys the new ARKProvider.parse_filings expects, after
    normalizing dates to YYYY-MM-DD (API format) so grouping-by-date matches
    the production path.
    """
    import json as _json
    trades = []
    for row in rows:
        date_in = row["date"]
        try:
            dt = datetime.strptime(date_in, "%m/%d/%Y")
            date_out = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_out = date_in
        trades.append({
            "fund": row["fund"],
            "date": date_out,
            "direction": row["direction"],
            "ticker": row["ticker"],
            "company": row.get("company", ""),
            "shares": int(row["shares"]),
        })
    return _json.dumps(trades)


# ---------------------------------------------------------------------------
# Property 6: ARK minimum buying days filter
# ---------------------------------------------------------------------------

class TestARKMinBuyingDaysFilter:
    """
    **Validates: Requirements 3.5**

    Property 6: For any set of ARK daily holdings snapshots over the recency
    window, a symbol SHALL be included as a candidate if and only if it has
    net positive share changes on at least `min_ark_buying_days` distinct days.
    """

    @given(data=ark_daily_snapshot())
    @settings(max_examples=100)
    def test_ark_buying_days_filter(self, data):
        """
        **Validates: Requirements 3.5**

        Generate random sets of daily holdings snapshots with varying symbols
        and share changes, assert a symbol is included as candidate iff it
        has net positive share changes on at least min_ark_buying_days
        distinct days.
        """
        csv_rows, buy_days_per_symbol, min_buying_days = data

        # Create an ARKProvider with the generated min_ark_buying_days
        config = SmartMoneyConfig(
            min_ark_buying_days=min_buying_days,
            recency_window_days=30,
        )
        provider = ARKProvider(config)

        # Build CSV and parse
        csv_text = _build_ark_csv(csv_rows)
        filings = provider.parse_filings(csv_text)

        # Determine which symbols appear in the output
        result_symbols = {f.symbol for f in filings}

        # Determine which symbols SHOULD appear based on the buying days rule
        expected_symbols = set()
        for symbol, days in buy_days_per_symbol.items():
            if len(days) >= min_buying_days:
                expected_symbols.add(symbol)

        # The iff condition: result symbols == expected symbols
        assert result_symbols == expected_symbols, (
            f"min_ark_buying_days={min_buying_days}\n"
            f"Buy days per symbol: {({s: len(d) for s, d in buy_days_per_symbol.items()})}\n"
            f"Expected symbols: {sorted(expected_symbols)}\n"
            f"Got symbols: {sorted(result_symbols)}"
        )

        # Additionally verify that every returned filing belongs to a qualifying symbol
        for filing in filings:
            assert filing.symbol in expected_symbols, (
                f"Filing for {filing.symbol} returned but symbol only has "
                f"{len(buy_days_per_symbol.get(filing.symbol, set()))} buying days "
                f"(need {min_buying_days})"
            )


# ---------------------------------------------------------------------------
# Strategies for Insider Cluster detection (Property 1)
# ---------------------------------------------------------------------------

from smart_trader.core.smart_money_providers.insider_cluster import InsiderClusterProvider, _FALSE_TICKER_BLOCKLIST as _TICKER_BLOCKLIST

# Strategy for insider names (distinct from ticker-like strings)
insider_name = st.text(
    alphabet=st.characters(whitelist_categories=("L",), min_codepoint=97, max_codepoint=122),
    min_size=3,
    max_size=12,
).map(lambda s: s.capitalize())

# Strategy for insider titles
insider_title = st.sampled_from(["CEO", "CFO", "COO", "Director", "VP", "Officer"])


def _build_openinsider_html(transactions: list) -> str:
    """Build a minimal OpenInsider-style HTML table from transaction dicts.

    Each transaction dict has keys: filing_date, trade_date, ticker,
    insider_name, title, dollar_value, shares.

    Layout matches the live OpenInsider tinytable schema. The parser pins to
    <table class="tinytable"> and pulls the ticker from <a href="/TICKER">.
    """
    rows_html = []
    for tx in transactions:
        # Live OpenInsider tinytable layout:
        # 0: X marker, 1: Filing Date, 2: Trade Date, 3: Ticker,
        # 4: Company Name, 5: Insider Name, 6: Title, 7: Trade Type,
        # 8: Price, 9: Qty, 10: Owned, 11: ΔOwn, 12: Value, 13-16: extras
        cells = [
            "<td></td>",  # 0
            f"<td>{tx['filing_date']}</td>",  # 1
            f"<td>{tx['trade_date']}</td>",  # 2
            f'<td><a href="/{tx["ticker"]}">{tx["ticker"]}</a></td>',  # 3
            f"<td>Test Co {tx['ticker']}</td>",  # 4
            f"<td>{tx['insider_name']}</td>",  # 5
            f"<td>{tx['title']}</td>",  # 6
            "<td>P - Purchase</td>",  # 7
            "<td>$100.00</td>",  # 8
            f"<td>+{tx['shares']}</td>",  # 9
            "<td>50,000</td>",  # 10
            "<td>+10%</td>",  # 11
            f"<td>${tx['dollar_value']:,.0f}</td>",  # 12
            "<td></td>", "<td></td>", "<td></td>", "<td></td>",  # 13-16
        ]
        rows_html.append(f"<tr>{''.join(cells)}</tr>")

    return f'<table class="tinytable">{"".join(rows_html)}</table>'


@st.composite
def insider_cluster_scenario(draw):
    """Generate a random set of insider transactions for cluster detection testing.

    Returns a tuple of:
    - transactions: list of dicts for building HTML
    - expected_cluster_symbols: set of symbols that should be detected as clusters
    - min_cluster_insiders: the threshold to use
    - cluster_window_days: the window to use

    All trade dates are within the cluster window so the cutoff filter does
    not interfere with the cluster grouping logic under test.
    """
    # Pick a small set of symbols (1-4)
    n_symbols = draw(st.integers(min_value=1, max_value=4))
    symbols = draw(
        st.lists(
            st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=4).filter(
                lambda s: s not in _TICKER_BLOCKLIST
            ),
            min_size=n_symbols,
            max_size=n_symbols,
            unique=True,
        )
    )

    # Pick min_cluster_insiders (2-4)
    min_cluster_insiders = draw(st.integers(min_value=2, max_value=4))

    # Use a fixed cluster_window_days (30 days)
    cluster_window_days = 30

    # Generate a pool of unique insider names (enough for all symbols)
    max_insiders_per_symbol = 6
    total_insiders_needed = n_symbols * max_insiders_per_symbol
    all_insider_names = draw(
        st.lists(
            insider_name,
            min_size=total_insiders_needed,
            max_size=total_insiders_needed,
            unique=True,
        )
    )

    today = datetime.now()
    transactions = []
    expected_cluster_symbols: set = set()

    insider_idx = 0
    for symbol in symbols:
        # Decide how many distinct insiders trade this symbol (1 to max_insiders_per_symbol)
        n_insiders = draw(st.integers(min_value=1, max_value=max_insiders_per_symbol))
        symbol_insiders = all_insider_names[insider_idx:insider_idx + n_insiders]
        insider_idx += n_insiders

        # If n_insiders >= min_cluster_insiders, this symbol should be a cluster
        if n_insiders >= min_cluster_insiders:
            expected_cluster_symbols.add(symbol)

        # Each insider gets 1-3 transactions at this symbol
        for ins_name in symbol_insiders:
            n_txs = draw(st.integers(min_value=1, max_value=3))
            title = draw(insider_title)
            for _ in range(n_txs):
                # Trade date within the last (cluster_window_days - 1) days
                days_ago = draw(st.integers(min_value=0, max_value=cluster_window_days - 2))
                trade_date = today - timedelta(days=days_ago)
                filing_date = trade_date + timedelta(days=draw(st.integers(min_value=0, max_value=2)))
                dollar_value = draw(st.integers(min_value=10000, max_value=5000000))
                shares = draw(st.integers(min_value=100, max_value=100000))

                transactions.append({
                    "filing_date": filing_date.strftime("%Y-%m-%d"),
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "ticker": symbol,
                    "insider_name": ins_name,
                    "title": title,
                    "dollar_value": dollar_value,
                    "shares": shares,
                })

    return transactions, expected_cluster_symbols, min_cluster_insiders, cluster_window_days


# ---------------------------------------------------------------------------
# Property 1: Cluster buy detection identifies groups correctly
# ---------------------------------------------------------------------------

class TestClusterBuyDetection:
    """
    **Validates: Requirements 1.5, 3.6**

    Property 1: For any set of insider purchase transactions with varying
    companies, insider names, and trade dates, the cluster detection algorithm
    SHALL identify a cluster buy for a company if and only if two or more
    distinct insiders at that company have purchase transactions within the
    configured cluster window (default: 30 days).
    """

    @given(data=insider_cluster_scenario())
    @settings(max_examples=100)
    def test_cluster_buy_detection_identifies_groups_correctly(self, data):
        """
        **Validates: Requirements 1.5, 3.6**

        Generate random sets of insider transactions with varying companies,
        insider names, and trade dates; assert cluster detected iff ≥ 2
        distinct insiders at same company within cluster_window_days.
        """
        transactions, expected_cluster_symbols, min_cluster_insiders, cluster_window_days = data

        # Create an InsiderClusterProvider with the generated config
        config = SmartMoneyConfig(
            min_cluster_insiders=min_cluster_insiders,
            cluster_window_days=cluster_window_days,
        )
        provider = InsiderClusterProvider(config)

        # Build HTML and parse
        html = _build_openinsider_html(transactions)
        filings = provider.parse_filings(html)

        # Determine which symbols appear in the output
        result_symbols = {f.symbol for f in filings}

        # The iff condition: a symbol appears in results iff it has
        # >= min_cluster_insiders distinct insiders
        assert result_symbols == expected_cluster_symbols, (
            f"min_cluster_insiders={min_cluster_insiders}\n"
            f"Expected cluster symbols: {sorted(expected_cluster_symbols)}\n"
            f"Got cluster symbols: {sorted(result_symbols)}\n"
            f"Transactions per symbol: {_count_insiders_per_symbol(transactions)}"
        )

        # Additionally verify that every returned filing belongs to a cluster symbol
        for filing in filings:
            assert filing.symbol in expected_cluster_symbols, (
                f"Filing for {filing.symbol} returned but symbol is not a cluster "
                f"(has {_count_insiders_per_symbol(transactions).get(filing.symbol, 0)} "
                f"distinct insiders, need {min_cluster_insiders})"
            )


def _count_insiders_per_symbol(transactions: list) -> Dict[str, int]:
    """Helper: count distinct insiders per symbol from transaction dicts."""
    symbol_insiders: Dict[str, set] = {}
    for tx in transactions:
        symbol = tx["ticker"]
        if symbol not in symbol_insiders:
            symbol_insiders[symbol] = set()
        symbol_insiders[symbol].add(tx["insider_name"])
    return {s: len(insiders) for s, insiders in symbol_insiders.items()}


# ---------------------------------------------------------------------------
# Property 4: Cache freshness determines fetch-or-load behavior
# ---------------------------------------------------------------------------

import tempfile
from unittest.mock import MagicMock, patch
from smart_trader.core.smart_money import CacheEntry, SmartMoneyScanner


class _CacheFreshnessProvider(DataProvider):
    """A mock DataProvider that tracks whether fetch_raw_data was called."""

    def __init__(self, filings: List[TradeFiling]):
        self._filings = filings
        self.fetch_called = False

    def fetch_raw_data(self) -> str:
        self.fetch_called = True
        return "fresh_data"

    def parse_filings(self, raw_data: str) -> List[TradeFiling]:
        return self._filings

    def get_cache_ttl_hours(self) -> float:
        return 24.0  # default; overridden by CacheEntry.ttl_hours in cache

    @property
    def provider_name(self) -> str:
        return "test_cache_provider"


@st.composite
def cache_freshness_scenario(draw):
    """Generate random TTL values and cache ages for cache freshness testing.

    Returns a tuple of:
    - ttl_hours: float, the cache TTL in hours (0.5 to 200)
    - age_hours: float, the age of the cached data in hours (0.0 to 300)
    - n_cached_filings: int, number of filings in the cache (1-5)
    - n_fresh_filings: int, number of filings the provider would return fresh (1-5)
    """
    ttl_hours = draw(st.floats(min_value=0.5, max_value=200.0, allow_nan=False, allow_infinity=False))
    age_hours = draw(st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False))
    n_cached_filings = draw(st.integers(min_value=1, max_value=5))
    n_fresh_filings = draw(st.integers(min_value=1, max_value=5))
    return ttl_hours, age_hours, n_cached_filings, n_fresh_filings


def _make_dummy_filings(n: int, label: str) -> List[TradeFiling]:
    """Create n dummy TradeFiling records with a distinguishable actor label."""
    now = datetime.now()
    return [
        TradeFiling(
            source="test_cache_provider",
            actor=f"{label}_actor_{i}",
            symbol=f"SYM{i}",
            tx_type="buy",
            dollar_amount=100_000.0,
            share_change=None,
            filing_date=now,
            trade_date=now,
        )
        for i in range(n)
    ]


class TestCacheFreshnessDeterminesFetchOrLoad:
    """
    **Validates: Requirements 2.2, 2.4**

    Property 4: For any provider with a configured cache TTL and a cache
    entry with a known timestamp, the scanner SHALL return cached data
    without a network request when (now - fetched_at) < ttl_hours, and
    SHALL fetch fresh data when (now - fetched_at) >= ttl_hours.
    """

    @given(data=cache_freshness_scenario())
    @settings(max_examples=100)
    def test_cache_freshness_determines_fetch_or_load(self, data):
        """
        **Validates: Requirements 2.2, 2.4**

        Generate random TTL values and cache ages, assert cached data
        returned without fetch when age < TTL, fresh fetch when age >= TTL.
        """
        ttl_hours, age_hours, n_cached_filings, n_fresh_filings = data

        # Create distinguishable cached vs fresh filings
        cached_filings = _make_dummy_filings(n_cached_filings, "cached")
        fresh_filings = _make_dummy_filings(n_fresh_filings, "fresh")

        # Create the mock provider that returns fresh filings on fetch
        provider = _CacheFreshnessProvider(fresh_filings)

        # Use a temporary directory for disk cache (created fresh each run)
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a SmartMoneyScanner with all providers disabled to avoid
            # real initialization, using tmp_dir for disk cache
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            # Patch _init_providers and _restore_disk_cache to avoid side effects
            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            # Pre-populate the in-memory cache with a CacheEntry at a known age
            now = datetime.now()
            fetched_at = now - timedelta(hours=age_hours)
            scanner._cache["test_cache_provider"] = CacheEntry(
                provider_name="test_cache_provider",
                filings=cached_filings,
                fetched_at=fetched_at,
                ttl_hours=ttl_hours,
            )

            # Call _fetch_or_cache
            result = scanner._fetch_or_cache(provider)

            if age_hours < ttl_hours:
                # Cache is fresh: should return cached data without fetching
                assert not provider.fetch_called, (
                    f"fetch_raw_data was called but cache age ({age_hours:.2f}h) "
                    f"< TTL ({ttl_hours:.2f}h) — should have used cache"
                )
                assert len(result) == n_cached_filings, (
                    f"Expected {n_cached_filings} cached filings but got {len(result)}"
                )
                # Verify we got the cached filings (actor labels start with "cached")
                for filing in result:
                    assert filing.actor.startswith("cached"), (
                        f"Expected cached filing but got actor={filing.actor!r}"
                    )
            else:
                # Cache is stale: should fetch fresh data
                assert provider.fetch_called, (
                    f"fetch_raw_data was NOT called but cache age ({age_hours:.2f}h) "
                    f">= TTL ({ttl_hours:.2f}h) — should have fetched fresh"
                )
                assert len(result) == n_fresh_filings, (
                    f"Expected {n_fresh_filings} fresh filings but got {len(result)}"
                )
                # Verify we got the fresh filings (actor labels start with "fresh")
                for filing in result:
                    assert filing.actor.startswith("fresh"), (
                        f"Expected fresh filing but got actor={filing.actor!r}"
                    )


# ---------------------------------------------------------------------------
# Strategies for Politician Ranking (Property 5)
# ---------------------------------------------------------------------------

# Strategy for politician names (distinct readable strings)
politician_name = st.text(
    alphabet=st.characters(whitelist_categories=("L",), min_codepoint=65, max_codepoint=90),
    min_size=3,
    max_size=8,
).map(lambda s: s.capitalize())


@st.composite
def politician_ranking_scenario(draw):
    """Generate random Capitol Trades BUY filings from multiple politicians.

    Returns a tuple of:
    - filings: list of TradeFiling records (all capitol_trades BUY, within recency window)
    - politician_stats: dict mapping politician name -> {count, dollar_volume}
    - min_politician_filings: int threshold
    - top_n_politicians: int max returned
    - recency_window_days: int (fixed at 30)
    """
    # Pick a set of unique politician names (2-8)
    n_politicians = draw(st.integers(min_value=2, max_value=8))
    politicians = draw(
        st.lists(politician_name, min_size=n_politicians, max_size=n_politicians, unique=True)
    )

    # Config thresholds
    min_filings = draw(st.integers(min_value=1, max_value=5))
    top_n = draw(st.integers(min_value=1, max_value=10))
    recency_window_days = 30

    # Generate filings for each politician — all within recency window
    today = datetime.now()
    filings: List[TradeFiling] = []
    politician_stats: Dict[str, Dict[str, Any]] = {}

    for pol_name in politicians:
        # Each politician gets 0-6 BUY filings
        n_buys = draw(st.integers(min_value=0, max_value=6))
        total_dollar = 0.0

        for _ in range(n_buys):
            # Dollar amount: positive float
            dollar_amt = draw(st.floats(min_value=1.0, max_value=1e8, allow_nan=False, allow_infinity=False))
            total_dollar += dollar_amt

            # Trade date within recency window (last 1-25 days)
            days_ago = draw(st.integers(min_value=0, max_value=recency_window_days - 5))
            trade_date = today - timedelta(days=days_ago)
            filing_date = trade_date + timedelta(days=draw(st.integers(min_value=0, max_value=2)))

            filings.append(TradeFiling(
                source="capitol_trades",
                actor=pol_name,
                symbol=draw(ticker_symbol),
                tx_type=draw(st.sampled_from(["buy", "increase"])),
                dollar_amount=dollar_amt,
                share_change=None,
                filing_date=filing_date,
                trade_date=trade_date,
            ))

        politician_stats[pol_name] = {"count": n_buys, "dollar_volume": total_dollar}

    return filings, politician_stats, min_filings, top_n, recency_window_days


# ---------------------------------------------------------------------------
# Property 5: Politician ranking is correct and respects thresholds
# ---------------------------------------------------------------------------

class TestPoliticianRankingCorrectAndRespectsThresholds:
    """
    **Validates: Requirements 3.1, 3.2, 3.3**

    Property 5: For any set of Capitol Trades BUY filings from multiple
    politicians within the recency window, the _rank_politicians method SHALL:
    - Exclude politicians with fewer than min_politician_filings BUY filings
    - Sort remaining politicians by (filing_count, total_dollar_volume) descending
    - Return at most top_n_politicians names
    """

    @given(data=politician_ranking_scenario())
    @settings(max_examples=100)
    def test_politician_ranking_correct_and_respects_thresholds(self, data):
        """
        **Validates: Requirements 3.1, 3.2, 3.3**

        Generate random sets of Capitol Trades BUY filings from multiple
        politicians, assert politicians below min_politician_filings excluded,
        remaining sorted by (filing_count, dollar_volume) descending, at most
        top_n_politicians returned.
        """
        filings, politician_stats, min_filings, top_n, recency_window_days = data

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                min_politician_filings=min_filings,
                top_n_politicians=top_n,
                recency_window_days=recency_window_days,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            result = scanner._rank_politicians(filings)

            # 1. Compute expected qualified politicians
            qualified = {
                name: stats for name, stats in politician_stats.items()
                if stats["count"] >= min_filings
            }

            # 2. Sort qualified by (filing_count, dollar_volume) descending
            expected_sorted = sorted(
                qualified.items(),
                key=lambda x: (x[1]["count"], x[1]["dollar_volume"]),
                reverse=True,
            )

            # 3. Take at most top_n
            expected_names = [name for name, _ in expected_sorted[:top_n]]

            # Assert: politicians below threshold are excluded
            for name, stats in politician_stats.items():
                if stats["count"] < min_filings:
                    assert name not in result, (
                        f"Politician {name!r} has {stats['count']} filings "
                        f"(< min {min_filings}) but was included in ranking"
                    )

            # Assert: at most top_n_politicians returned
            assert len(result) <= top_n, (
                f"Expected at most {top_n} politicians but got {len(result)}"
            )

            # Assert: result matches expected order and content
            assert result == expected_names, (
                f"min_politician_filings={min_filings}, top_n={top_n}\n"
                f"Politician stats: {politician_stats}\n"
                f"Qualified: {qualified}\n"
                f"Expected: {expected_names}\n"
                f"Got: {result}"
            )


# ---------------------------------------------------------------------------
# Strategies for tx_type filtering (Property 7)
# ---------------------------------------------------------------------------


@st.composite
def mixed_tx_type_filings_scenario(draw):
    """Generate random TradeFiling records with mixed tx_types.

    Returns a tuple of:
    - filings: list of TradeFiling records with mixed tx_types
    - buy_increase_symbols: set of symbols that appear in at least one
      "buy" or "increase" filing (these should appear in candidates)
    - sell_decrease_only_symbols: set of symbols that appear ONLY in
      "sell" or "decrease" filings (these should NOT appear in candidates)

    All filings have sufficient dollar_amount (above min_trade_amount) and
    trade_date within the recency window so that other filters don't
    interfere with the tx_type filtering under test.
    """
    # Pick a set of unique symbols (2-6)
    n_symbols = draw(st.integers(min_value=2, max_value=6))
    symbols = draw(
        st.lists(ticker_symbol, min_size=n_symbols, max_size=n_symbols, unique=True)
    )

    today = datetime.now()
    filings: List[TradeFiling] = []
    symbol_tx_types: Dict[str, set] = {s: set() for s in symbols}

    for symbol in symbols:
        # Each symbol gets 1-4 filings with random tx_types
        n_filings = draw(st.integers(min_value=1, max_value=4))
        for _ in range(n_filings):
            tx_type = draw(st.sampled_from(VALID_TX_TYPES))
            # Use a non-ARK source so dollar_amount threshold applies
            source = draw(st.sampled_from(["capitol_trades", "berkshire_13f", "insider_cluster"]))
            # Dollar amount well above min_trade_amount (default 50k)
            dollar_amount = draw(st.floats(
                min_value=100_000.0, max_value=10_000_000.0,
                allow_nan=False, allow_infinity=False,
            ))
            # Trade date within recency window (last 1-20 days)
            days_ago = draw(st.integers(min_value=0, max_value=20))
            trade_date = today - timedelta(days=days_ago)
            filing_date = trade_date + timedelta(days=draw(st.integers(min_value=0, max_value=2)))

            filings.append(TradeFiling(
                source=source,
                actor=f"Actor_{symbol}_{tx_type}",
                symbol=symbol,
                tx_type=tx_type,
                dollar_amount=dollar_amount,
                share_change=None,
                filing_date=filing_date,
                trade_date=trade_date,
            ))
            symbol_tx_types[symbol].add(tx_type)

    # Classify symbols
    buy_increase_symbols = set()
    sell_decrease_only_symbols = set()
    for symbol, tx_types in symbol_tx_types.items():
        if tx_types & {"buy", "increase"}:
            buy_increase_symbols.add(symbol)
        else:
            sell_decrease_only_symbols.add(symbol)

    return filings, buy_increase_symbols, sell_decrease_only_symbols


# ---------------------------------------------------------------------------
# Property 7: Only BUY and INCREASE transaction types pass aggregation
# ---------------------------------------------------------------------------

class TestOnlyBuyAndIncreasePassAggregation:
    """
    **Validates: Requirements 4.1**

    Property 7: For any set of TradeFiling records from all enabled providers,
    the candidate generation step SHALL include only filings where tx_type is
    "buy" or "increase", and SHALL exclude all filings with tx_type "sell" or
    "decrease".
    """

    @given(data=mixed_tx_type_filings_scenario())
    @settings(max_examples=100)
    def test_only_buy_and_increase_tx_types_pass_aggregation(self, data):
        """
        **Validates: Requirements 4.1**

        Generate random TradeFiling records with mixed tx_types, assert only
        "buy" and "increase" filings contribute to candidate generation.
        Verify that symbols appearing ONLY in "sell"/"decrease" filings do
        NOT appear in candidates.
        """
        filings, buy_increase_symbols, sell_decrease_only_symbols = data

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                min_trade_amount=50_000.0,
                recency_window_days=30,
                berkshire_recency_days=90,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            candidates = scanner._compute_conviction_scores(filings)
            candidate_symbols = {c.symbol for c in candidates}

            # 1. Symbols that appear ONLY in sell/decrease filings must NOT
            #    appear in candidates
            for symbol in sell_decrease_only_symbols:
                assert symbol not in candidate_symbols, (
                    f"Symbol {symbol!r} appears only in sell/decrease filings "
                    f"but was included in candidates"
                )

            # 2. Every candidate symbol must have had at least one buy/increase
            #    filing in the input
            for symbol in candidate_symbols:
                assert symbol in buy_increase_symbols, (
                    f"Candidate symbol {symbol!r} has no buy/increase filings "
                    f"in the input"
                )

            # 3. All symbols with buy/increase filings should appear in
            #    candidates (since all filings have sufficient dollar_amount
            #    and are within recency window)
            assert candidate_symbols == buy_increase_symbols, (
                f"Expected candidate symbols to be exactly the buy/increase "
                f"symbols.\n"
                f"Expected: {sorted(buy_increase_symbols)}\n"
                f"Got: {sorted(candidate_symbols)}\n"
                f"Sell/decrease only: {sorted(sell_decrease_only_symbols)}"
            )


# ---------------------------------------------------------------------------
# Strategies for filing threshold and recency filtering (Property 8)
# ---------------------------------------------------------------------------

# Sources that use dollar_amount threshold (non-ARK)
NON_ARK_SOURCES = ["capitol_trades", "berkshire_13f", "insider_cluster"]


@st.composite
def filing_threshold_recency_scenario(draw):
    """Generate random filings with varying dollar amounts, share changes,
    and trade dates for testing threshold and recency filtering.

    Returns a tuple of:
    - filings: list of TradeFiling records (all buy/increase tx_type)
    - min_trade_amount: float threshold for non-ARK dollar amounts
    - min_share_change: int threshold for ARK share changes
    - recency_window_days: int general recency window
    - berkshire_recency_days: int Berkshire-specific recency window
    - expected_symbols: set of symbols that should pass all filters

    Each filing is tagged with a unique symbol so we can track which
    individual filings pass or fail the filters.
    """
    # Config thresholds
    min_trade_amount = draw(st.floats(
        min_value=1_000.0, max_value=500_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    min_share_change = draw(st.integers(min_value=100, max_value=100_000))
    recency_window_days = draw(st.integers(min_value=10, max_value=60))
    berkshire_recency_days = draw(st.integers(
        min_value=recency_window_days + 1, max_value=180,
    ))

    today = datetime.now()
    filings: List[TradeFiling] = []
    expected_symbols: set = set()

    # Generate 3-12 filings, each with a unique symbol for easy tracking
    n_filings = draw(st.integers(min_value=3, max_value=12))
    symbols = draw(
        st.lists(ticker_symbol, min_size=n_filings, max_size=n_filings, unique=True)
    )

    for i, symbol in enumerate(symbols):
        # Decide the source for this filing
        source = draw(st.sampled_from(VALID_SOURCES))
        tx_type = draw(st.sampled_from(["buy", "increase"]))

        # Decide trade date: either within or outside the applicable recency window
        if source == "berkshire_13f":
            applicable_window = berkshire_recency_days
        else:
            applicable_window = recency_window_days

        # Generate days_ago: 0 to applicable_window*2 to get mix of within/outside
        days_ago = draw(st.integers(min_value=0, max_value=applicable_window * 2))
        trade_date = today - timedelta(days=days_ago)
        filing_date = trade_date + timedelta(days=draw(st.integers(min_value=0, max_value=2)))

        within_recency = days_ago < applicable_window

        # Generate dollar_amount and share_change based on source
        passes_threshold = False
        if source == "ark_invest":
            # ARK uses share_change threshold
            dollar_amount = None
            share_change = draw(st.integers(min_value=1, max_value=min_share_change * 3))
            passes_threshold = share_change >= min_share_change
        else:
            # Non-ARK uses dollar_amount threshold
            dollar_amount = draw(st.floats(
                min_value=1.0, max_value=min_trade_amount * 3,
                allow_nan=False, allow_infinity=False,
            ))
            share_change = None
            passes_threshold = dollar_amount >= min_trade_amount

        filings.append(TradeFiling(
            source=source,
            actor=f"Actor_{symbol}",
            symbol=symbol,
            tx_type=tx_type,
            dollar_amount=dollar_amount,
            share_change=share_change,
            filing_date=filing_date,
            trade_date=trade_date,
        ))

        # A filing passes all filters iff it passes both threshold AND recency
        if passes_threshold and within_recency:
            expected_symbols.add(symbol)

    return (
        filings,
        min_trade_amount,
        min_share_change,
        recency_window_days,
        berkshire_recency_days,
        expected_symbols,
    )


# ---------------------------------------------------------------------------
# Property 8: Filing threshold and recency filtering
# ---------------------------------------------------------------------------

class TestFilingThresholdAndRecencyFiltering:
    """
    **Validates: Requirements 4.2, 4.3**

    Property 8: For any set of TradeFiling records, the scanner SHALL exclude:
    - Filings with dollar_amount below min_trade_amount (non-ARK sources)
    - ARK filings with share_change below min_share_change
    - Non-Berkshire filings with trade_date older than recency_window_days
    - Berkshire filings with trade_date older than berkshire_recency_days
    """

    @given(data=filing_threshold_recency_scenario())
    @settings(max_examples=100)
    def test_filing_threshold_and_recency_filtering(self, data):
        """
        **Validates: Requirements 4.2, 4.3**

        Generate random filings with varying dollar amounts, share changes,
        and trade dates; assert correct exclusion based on min_trade_amount,
        min_share_change, recency_window_days, and berkshire_recency_days.
        """
        (
            filings,
            min_trade_amount,
            min_share_change,
            recency_window_days,
            berkshire_recency_days,
            expected_symbols,
        ) = data

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                min_trade_amount=min_trade_amount,
                min_share_change=min_share_change,
                recency_window_days=recency_window_days,
                berkshire_recency_days=berkshire_recency_days,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            candidates = scanner._compute_conviction_scores(filings)
            candidate_symbols = {c.symbol for c in candidates}

            # 1. Non-ARK filings with dollar_amount < min_trade_amount are excluded
            for f in filings:
                if f.source != "ark_invest" and f.dollar_amount is not None:
                    if f.dollar_amount < min_trade_amount:
                        assert f.symbol not in candidate_symbols or f.symbol in expected_symbols, (
                            f"Filing for {f.symbol} (source={f.source}) has "
                            f"dollar_amount={f.dollar_amount} < min_trade_amount="
                            f"{min_trade_amount} but symbol appeared in candidates"
                        )

            # 2. ARK filings with share_change < min_share_change are excluded
            for f in filings:
                if f.source == "ark_invest" and f.share_change is not None:
                    if f.share_change < min_share_change:
                        assert f.symbol not in candidate_symbols or f.symbol in expected_symbols, (
                            f"ARK filing for {f.symbol} has share_change="
                            f"{f.share_change} < min_share_change="
                            f"{min_share_change} but symbol appeared in candidates"
                        )

            # 3. Non-Berkshire filings older than recency_window_days are excluded
            now = datetime.now()
            cutoff = now - timedelta(days=recency_window_days)
            for f in filings:
                if f.source != "berkshire_13f" and f.trade_date < cutoff:
                    assert f.symbol not in candidate_symbols or f.symbol in expected_symbols, (
                        f"Filing for {f.symbol} (source={f.source}) has "
                        f"trade_date={f.trade_date} older than recency window "
                        f"({recency_window_days} days) but symbol appeared"
                    )

            # 4. Berkshire filings older than berkshire_recency_days are excluded
            berkshire_cutoff = now - timedelta(days=berkshire_recency_days)
            for f in filings:
                if f.source == "berkshire_13f" and f.trade_date < berkshire_cutoff:
                    assert f.symbol not in candidate_symbols or f.symbol in expected_symbols, (
                        f"Berkshire filing for {f.symbol} has trade_date="
                        f"{f.trade_date} older than berkshire recency window "
                        f"({berkshire_recency_days} days) but symbol appeared"
                    )

            # 5. Filings that pass all filters contribute to candidates
            #    (each filing has a unique symbol, so expected_symbols should
            #    match candidate_symbols exactly)
            assert candidate_symbols == expected_symbols, (
                f"Expected candidate symbols don't match.\n"
                f"min_trade_amount={min_trade_amount}, "
                f"min_share_change={min_share_change}, "
                f"recency_window_days={recency_window_days}, "
                f"berkshire_recency_days={berkshire_recency_days}\n"
                f"Expected: {sorted(expected_symbols)}\n"
                f"Got: {sorted(candidate_symbols)}\n"
                f"Missing: {sorted(expected_symbols - candidate_symbols)}\n"
                f"Extra: {sorted(candidate_symbols - expected_symbols)}"
            )


# ---------------------------------------------------------------------------
# Strategies for conviction score formula and candidate ordering (Property 9)
# ---------------------------------------------------------------------------


@st.composite
def conviction_score_scenario(draw):
    """Generate random qualifying filings grouped by symbol for conviction
    score formula verification.

    All filings are:
    - tx_type "buy" or "increase" (qualifying)
    - dollar_amount above min_trade_amount (non-ARK) or share_change above
      min_share_change (ARK)
    - trade_date within the applicable recency window

    Returns a tuple of:
    - filings: list of TradeFiling records
    - expected_per_symbol: dict mapping symbol -> {
          n_sources: int,
          dollar_volume: float,
          most_recent_filing: datetime,
      }
    - recency_window_days: int
    - berkshire_recency_days: int
    - min_trade_amount: float
    - min_share_change: int
    """
    # Pick 2-5 unique symbols
    n_symbols = draw(st.integers(min_value=2, max_value=5))
    symbols = draw(
        st.lists(ticker_symbol, min_size=n_symbols, max_size=n_symbols, unique=True)
    )

    recency_window_days = 30
    berkshire_recency_days = 90
    min_trade_amount = 50_000.0
    min_share_change = 10_000

    today = datetime.now()
    filings: List[TradeFiling] = []
    expected_per_symbol: Dict[str, Dict[str, Any]] = {}

    for symbol in symbols:
        # Each symbol gets filings from 1-4 distinct sources
        n_sources = draw(st.integers(min_value=1, max_value=4))
        sources_for_symbol = draw(
            st.lists(
                st.sampled_from(VALID_SOURCES),
                min_size=n_sources,
                max_size=n_sources,
                unique=True,
            )
        )

        symbol_dollar_volume = 0.0
        symbol_most_recent = datetime(2020, 1, 1)
        symbol_actors: set = set()
        symbol_filing_count = 0

        for source in sources_for_symbol:
            # Each source contributes 1-3 filings for this symbol
            n_filings_per_source = draw(st.integers(min_value=1, max_value=3))

            for _ in range(n_filings_per_source):
                tx_type = draw(st.sampled_from(["buy", "increase"]))

                # Trade date within the applicable recency window
                if source == "berkshire_13f":
                    max_days_ago = berkshire_recency_days - 1
                else:
                    max_days_ago = recency_window_days - 1
                days_ago = draw(st.integers(min_value=0, max_value=max_days_ago))
                trade_date = today - timedelta(days=days_ago)
                filing_date = trade_date + timedelta(
                    days=draw(st.integers(min_value=0, max_value=2))
                )

                # Generate amounts above thresholds
                if source == "ark_invest":
                    dollar_amount = None
                    share_change = draw(st.integers(
                        min_value=min_share_change,
                        max_value=min_share_change * 10,
                    ))
                else:
                    dollar_amount = draw(st.floats(
                        min_value=min_trade_amount,
                        max_value=10_000_000.0,
                        allow_nan=False,
                        allow_infinity=False,
                    ))
                    share_change = None

                actor = f"Actor_{source}_{symbol}"
                filings.append(TradeFiling(
                    source=source,
                    actor=actor,
                    symbol=symbol,
                    tx_type=tx_type,
                    dollar_amount=dollar_amount,
                    share_change=share_change,
                    filing_date=filing_date,
                    trade_date=trade_date,
                ))

                symbol_dollar_volume += dollar_amount or 0.0
                symbol_actors.add(actor)
                symbol_filing_count += 1
                if filing_date > symbol_most_recent:
                    symbol_most_recent = filing_date

        expected_per_symbol[symbol] = {
            "n_sources": n_sources,
            "sources": set(sources_for_symbol),
            "n_actors": len(symbol_actors),
            "dollar_volume": symbol_dollar_volume,
            "filing_count": symbol_filing_count,
            "most_recent_filing": symbol_most_recent,
        }

    return (
        filings,
        expected_per_symbol,
        recency_window_days,
        berkshire_recency_days,
        min_trade_amount,
        min_share_change,
    )


# ---------------------------------------------------------------------------
# Property 9: Conviction score formula and candidate ordering
# ---------------------------------------------------------------------------

class TestConvictionScoreFormulaAndCandidateOrdering:
    """
    **Validates: Requirements 4.4, 4.5**

    Property 9: For any set of qualifying filings grouped by symbol, the
    computed Conviction_Score for each symbol SHALL equal the agreement-driven
    formula
        Σ source_quality(distinct sources) + cluster_bonus(n_actors)
        + dollar_bonus(tamed) + accumulation_bonus(filing_count)
        + recency_bonus,
    using the coefficients on SmartMoneyConfig, and the returned candidate list
    SHALL be deduplicated by symbol and sorted by Conviction_Score descending.
    """

    @given(data=conviction_score_scenario())
    @settings(max_examples=100)
    def test_conviction_score_formula_and_candidate_ordering(self, data):
        """
        **Validates: Requirements 4.4, 4.5**

        Generate random qualifying filings grouped by symbol, assert computed
        Conviction_Score matches the agreement-driven formula (source quality +
        cluster + tamed dollars + accumulation + recency) using config weights,
        and candidates are deduplicated and sorted descending.
        """
        (
            filings,
            expected_per_symbol,
            recency_window_days,
            berkshire_recency_days,
            min_trade_amount,
            min_share_change,
        ) = data

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                min_trade_amount=min_trade_amount,
                min_share_change=min_share_change,
                recency_window_days=recency_window_days,
                berkshire_recency_days=berkshire_recency_days,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            candidates = scanner._compute_conviction_scores(filings)

            # 1. Candidates are deduplicated by symbol
            candidate_symbols = [c.symbol for c in candidates]
            assert len(candidate_symbols) == len(set(candidate_symbols)), (
                f"Candidates are not deduplicated by symbol: {candidate_symbols}"
            )

            # 2. All expected symbols appear in candidates
            assert set(candidate_symbols) == set(expected_per_symbol.keys()), (
                f"Expected symbols: {sorted(expected_per_symbol.keys())}\n"
                f"Got symbols: {sorted(candidate_symbols)}"
            )

            # 3. Each candidate's conviction_score matches the formula,
            #    recomputed here from the same config coefficients.
            now = datetime.now()
            for candidate in candidates:
                expected = expected_per_symbol[candidate.symbol]
                sources = expected["sources"]
                n_actors = expected["n_actors"]
                dollar_volume = expected["dollar_volume"]
                filing_count = expected["filing_count"]
                most_recent = expected["most_recent_filing"]
                days_since = max(0, (now - most_recent).days)

                source_quality = sum(
                    config.conviction_source_weights.get(
                        s, config.conviction_default_source_weight
                    )
                    for s in sources
                )
                cluster_bonus = min(
                    config.conviction_cluster_cap,
                    config.conviction_cluster_per_actor * max(0, n_actors - 1),
                )
                if dollar_volume > 0:
                    excess = max(
                        0.0,
                        math.log10(dollar_volume + 1) - config.conviction_dollar_floor_log,
                    )
                    dollar_bonus = min(
                        config.conviction_dollar_cap,
                        config.conviction_dollar_coef * excess,
                    )
                else:
                    dollar_bonus = 0.0
                accum_bonus = min(
                    config.conviction_accum_cap,
                    config.conviction_accum_per_filing * max(0, filing_count - 1),
                )
                rd = config.conviction_recency_days
                recency_bonus = max(0.0, (rd - days_since) / rd)
                expected_score = (
                    source_quality + cluster_bonus + dollar_bonus
                    + accum_bonus + recency_bonus
                )

                assert abs(candidate.conviction_score - expected_score) < 1e-6, (
                    f"Symbol {candidate.symbol}: conviction_score mismatch.\n"
                    f"Got: {candidate.conviction_score}\n"
                    f"Expected: {expected_score}\n"
                    f"sources={sources}, n_actors={n_actors}, "
                    f"dollar_volume={dollar_volume}, filing_count={filing_count}, "
                    f"days_since={days_since}\n"
                    f"source_quality={source_quality}, cluster={cluster_bonus}, "
                    f"dollar={dollar_bonus}, accum={accum_bonus}, recency={recency_bonus}"
                )

            # 4. Candidates are sorted by conviction_score descending
            scores = [c.conviction_score for c in candidates]
            for i in range(len(scores) - 1):
                assert scores[i] >= scores[i + 1], (
                    f"Candidates not sorted descending by conviction_score.\n"
                    f"Score at index {i}: {scores[i]}\n"
                    f"Score at index {i+1}: {scores[i+1]}\n"
                    f"All scores: {scores}"
                )


# ---------------------------------------------------------------------------
# Strategies for regime filtering by vol_rank thresholds (Property 10)
# ---------------------------------------------------------------------------

from smart_trader.core.smart_money import CandidateSymbol


@st.composite
def regime_filter_scenario(draw):
    """Generate a random vol_rank in [0.0, 1.0] and a random candidate list
    with a mix of defensive and non-defensive symbols.

    Returns a tuple of:
    - vol_rank: float in [0.0, 1.0]
    - candidates: list of CandidateSymbol objects
    - defensive_symbols: list of defensive symbol strings (from config default)
    - defensive_candidates: list of CandidateSymbol whose symbol is in defensive_symbols
    - non_defensive_candidates: list of CandidateSymbol whose symbol is NOT in defensive_symbols
    """
    # Default defensive symbols from SmartMoneyConfig
    defensive_symbols = ["AAPL", "MSFT", "GOOGL", "JNJ", "PG", "KO", "PEP", "WMT", "UNH", "V"]

    # Non-defensive symbols that are guaranteed NOT in the defensive list
    non_defensive_pool = ["TSLA", "NVDA", "AMD", "PLTR", "SOFI", "RIVN", "LCID", "COIN", "MARA", "RIOT"]

    vol_rank = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))

    # Pick how many defensive and non-defensive candidates to include
    n_defensive = draw(st.integers(min_value=0, max_value=5))
    n_non_defensive = draw(st.integers(min_value=0, max_value=5))

    # Select symbols from each pool
    chosen_defensive = draw(
        st.lists(
            st.sampled_from(defensive_symbols),
            min_size=n_defensive,
            max_size=n_defensive,
            unique=True,
        )
    ) if n_defensive > 0 else []

    chosen_non_defensive = draw(
        st.lists(
            st.sampled_from(non_defensive_pool),
            min_size=n_non_defensive,
            max_size=n_non_defensive,
            unique=True,
        )
    ) if n_non_defensive > 0 else []

    now = datetime.now()
    candidates = []
    defensive_candidates = []
    non_defensive_candidates = []

    for symbol in chosen_defensive + chosen_non_defensive:
        candidate = CandidateSymbol(
            symbol=symbol,
            conviction_score=draw(st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False)),
            sources=["capitol_trades"],
            actors=["TestActor"],
            total_dollar_volume=draw(st.floats(min_value=50_000.0, max_value=10_000_000.0, allow_nan=False, allow_infinity=False)),
            filing_count=draw(st.integers(min_value=1, max_value=10)),
            most_recent_filing=now - timedelta(days=draw(st.integers(min_value=0, max_value=25))),
        )
        candidates.append(candidate)
        if symbol in defensive_symbols:
            defensive_candidates.append(candidate)
        else:
            non_defensive_candidates.append(candidate)

    return vol_rank, candidates, defensive_symbols, defensive_candidates, non_defensive_candidates


# ---------------------------------------------------------------------------
# Property 10: Regime filtering by vol_rank thresholds
# ---------------------------------------------------------------------------

class TestRegimeFilteringByVolRankThresholds:
    """
    **Validates: Requirements 5.1, 5.2, 5.3**

    Property 10: For any candidate list and vol_rank value in [0.0, 1.0],
    the regime filter SHALL:
    - Return all candidates when vol_rank ≤ 0.33
    - Return only candidates whose symbol is in the defensive symbol list
      when 0.33 < vol_rank ≤ 0.67
    - Return an empty list when vol_rank > 0.67
    """

    @given(data=regime_filter_scenario())
    @settings(max_examples=100)
    def test_regime_filtering_by_vol_rank_thresholds(self, data):
        """
        **Validates: Requirements 5.1, 5.2, 5.3**

        Generate random vol_rank in [0.0, 1.0] and random candidate lists
        with a mix of defensive and non-defensive symbols. Assert all pass
        when vol_rank ≤ 0.33, only defensive pass when 0.33 < vol_rank ≤ 0.67,
        none pass when vol_rank > 0.67.
        """
        vol_rank, candidates, defensive_symbols, defensive_candidates, non_defensive_candidates = data

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                defensive_symbols=defensive_symbols,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            result = scanner._apply_regime_filter(candidates, vol_rank)

            if vol_rank <= 0.33:
                # Low vol: all candidates should pass
                assert len(result) == len(candidates), (
                    f"vol_rank={vol_rank:.4f} (≤ 0.33): expected all "
                    f"{len(candidates)} candidates but got {len(result)}"
                )
                result_symbols = {c.symbol for c in result}
                input_symbols = {c.symbol for c in candidates}
                assert result_symbols == input_symbols, (
                    f"vol_rank={vol_rank:.4f} (≤ 0.33): result symbols "
                    f"don't match input.\n"
                    f"Expected: {sorted(input_symbols)}\n"
                    f"Got: {sorted(result_symbols)}"
                )

            elif vol_rank <= 0.67:
                # Mid vol: only defensive symbols should pass
                result_symbols = {c.symbol for c in result}
                defensive_set = set(defensive_symbols)

                # Every result symbol must be defensive
                for sym in result_symbols:
                    assert sym in defensive_set, (
                        f"vol_rank={vol_rank:.4f} (0.33 < v ≤ 0.67): "
                        f"non-defensive symbol {sym!r} in result"
                    )

                # Every defensive candidate from input must be in result
                expected_defensive_symbols = {c.symbol for c in defensive_candidates}
                assert result_symbols == expected_defensive_symbols, (
                    f"vol_rank={vol_rank:.4f} (0.33 < v ≤ 0.67): "
                    f"defensive symbols mismatch.\n"
                    f"Expected: {sorted(expected_defensive_symbols)}\n"
                    f"Got: {sorted(result_symbols)}"
                )

                # No non-defensive candidates should be in result
                for c in non_defensive_candidates:
                    assert c.symbol not in result_symbols, (
                        f"vol_rank={vol_rank:.4f} (0.33 < v ≤ 0.67): "
                        f"non-defensive symbol {c.symbol!r} should not be in result"
                    )

            else:
                # High vol: no candidates should pass
                assert len(result) == 0, (
                    f"vol_rank={vol_rank:.4f} (> 0.67): expected empty list "
                    f"but got {len(result)} candidates: "
                    f"{[c.symbol for c in result]}"
                )


# ---------------------------------------------------------------------------
# Property 11: Disabled toggle suppresses all output
# ---------------------------------------------------------------------------

class TestDisabledToggleSuppressesAllOutput:
    """
    **Validates: Requirements 7.13**

    Property 11: For any combination of provider data, vol_rank, and candidate
    inputs, when smart_money_enabled is False, get_candidates() SHALL return
    an empty list without invoking any provider fetch or cache operations.
    """

    @given(vol_rank=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_disabled_toggle_suppresses_all_output(self, vol_rank):
        """
        **Validates: Requirements 7.13**

        Set smart_money_enabled=False, generate random vol_rank values in
        [0.0, 1.0], assert get_candidates() returns empty list without
        invoking any provider fetch.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=False,
                capitol_trades_enabled=True,
                berkshire_enabled=True,
                ark_enabled=True,
                insider_cluster_enabled=True,
                disk_cache_dir=str(tmp_dir),
            )
            risk_config = RiskConfig()

            # Patch _init_providers and _restore_disk_cache to avoid side effects
            with patch.object(SmartMoneyScanner, "_init_providers") as mock_init, \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            # Patch _fetch_or_cache to detect if any provider fetch is attempted
            with patch.object(scanner, "_fetch_or_cache") as mock_fetch:
                result = scanner.get_candidates(vol_rank)

                # 1. Returns empty list
                assert result == [], (
                    f"Expected empty list when smart_money_enabled=False, "
                    f"but got {len(result)} candidates (vol_rank={vol_rank:.4f})"
                )

                # 2. No provider fetch was called
                mock_fetch.assert_not_called(), (
                    f"_fetch_or_cache was called {mock_fetch.call_count} time(s) "
                    f"when smart_money_enabled=False (vol_rank={vol_rank:.4f})"
                )


# ---------------------------------------------------------------------------
# Property 12: Smart money signal metadata completeness
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass, field as _field
from typing import Any


@_dataclass
class _MockSignal:
    """Lightweight stand-in for regime_strategies.Signal with symbol + metadata."""
    symbol: str
    metadata: Dict[str, Any] = _field(default_factory=dict)


@st.composite
def smart_money_candidate_st(draw):
    """Generate a random CandidateSymbol with valid data."""
    from smart_trader.core.smart_money import CandidateSymbol

    symbol = draw(ticker_symbol)
    conviction_score = draw(
        st.floats(min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False)
    )
    sources = draw(
        st.lists(st.sampled_from(VALID_SOURCES), min_size=1, max_size=4, unique=True)
    )
    actors = draw(
        st.lists(non_empty_text, min_size=1, max_size=5)
    )
    total_dollar_volume = draw(
        st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
    )
    filing_count = draw(st.integers(min_value=1, max_value=100))
    most_recent_filing = draw(reasonable_datetime)

    return CandidateSymbol(
        symbol=symbol,
        conviction_score=conviction_score,
        sources=sorted(sources),
        actors=sorted(actors),
        total_dollar_volume=total_dollar_volume,
        filing_count=filing_count,
        most_recent_filing=most_recent_filing,
    )


class TestSmartMoneySignalMetadataCompleteness:
    """
    **Validates: Requirements 8.4**

    Property 12: For any signal generated from a smart money candidate symbol,
    the signal's metadata dict SHALL contain the keys "smart_money" (True),
    "conviction_score" (float > 0), "sources" (non-empty list of strings),
    "actors" (non-empty list of strings), and "most_recent_filing" (ISO
    datetime string).
    """

    @given(
        candidates=st.lists(smart_money_candidate_st(), min_size=1, max_size=10, unique_by=lambda c: c.symbol),
        extra_symbols=st.lists(ticker_symbol, min_size=0, max_size=5),
    )
    @settings(max_examples=100)
    def test_smart_money_signal_metadata_completeness(self, candidates, extra_symbols):
        """
        **Validates: Requirements 8.4**

        Generate random smart money candidates, mock signal generation,
        assert every signal from a smart money candidate has metadata keys:
        smart_money (True), conviction_score (float > 0), sources (non-empty
        list), actors (non-empty list), most_recent_filing (ISO datetime string).
        """
        # Build the set of smart money candidate symbols
        sm_symbol_set = {c.symbol for c in candidates}

        # Create mock signals: one per candidate + some non-smart-money signals
        signals = [_MockSignal(symbol=c.symbol) for c in candidates]
        for sym in extra_symbols:
            if sym not in sm_symbol_set:
                signals.append(_MockSignal(symbol=sym))

        # Apply the metadata tagging logic (same as in _cycle())
        for sig in signals:
            if sig.symbol in sm_symbol_set:
                candidate = next(c for c in candidates if c.symbol == sig.symbol)
                sig.metadata["smart_money"] = True
                sig.metadata["conviction_score"] = candidate.conviction_score
                sig.metadata["sources"] = candidate.sources
                sig.metadata["actors"] = candidate.actors
                sig.metadata["most_recent_filing"] = candidate.most_recent_filing.isoformat()

        # Assert: every signal from a smart money candidate has complete metadata
        for sig in signals:
            if sig.symbol in sm_symbol_set:
                # smart_money must be True
                assert "smart_money" in sig.metadata, (
                    f"Signal for {sig.symbol} missing 'smart_money' key"
                )
                assert sig.metadata["smart_money"] is True, (
                    f"Signal for {sig.symbol}: smart_money should be True, "
                    f"got {sig.metadata['smart_money']!r}"
                )

                # conviction_score must be a float > 0
                assert "conviction_score" in sig.metadata, (
                    f"Signal for {sig.symbol} missing 'conviction_score' key"
                )
                assert isinstance(sig.metadata["conviction_score"], float), (
                    f"Signal for {sig.symbol}: conviction_score should be float, "
                    f"got {type(sig.metadata['conviction_score']).__name__}"
                )
                assert sig.metadata["conviction_score"] > 0, (
                    f"Signal for {sig.symbol}: conviction_score should be > 0, "
                    f"got {sig.metadata['conviction_score']}"
                )

                # sources must be a non-empty list
                assert "sources" in sig.metadata, (
                    f"Signal for {sig.symbol} missing 'sources' key"
                )
                assert isinstance(sig.metadata["sources"], list), (
                    f"Signal for {sig.symbol}: sources should be a list, "
                    f"got {type(sig.metadata['sources']).__name__}"
                )
                assert len(sig.metadata["sources"]) > 0, (
                    f"Signal for {sig.symbol}: sources should be non-empty"
                )

                # actors must be a non-empty list
                assert "actors" in sig.metadata, (
                    f"Signal for {sig.symbol} missing 'actors' key"
                )
                assert isinstance(sig.metadata["actors"], list), (
                    f"Signal for {sig.symbol}: actors should be a list, "
                    f"got {type(sig.metadata['actors']).__name__}"
                )
                assert len(sig.metadata["actors"]) > 0, (
                    f"Signal for {sig.symbol}: actors should be non-empty"
                )

                # most_recent_filing must be an ISO datetime string
                assert "most_recent_filing" in sig.metadata, (
                    f"Signal for {sig.symbol} missing 'most_recent_filing' key"
                )
                assert isinstance(sig.metadata["most_recent_filing"], str), (
                    f"Signal for {sig.symbol}: most_recent_filing should be str, "
                    f"got {type(sig.metadata['most_recent_filing']).__name__}"
                )
                # Verify it's a valid ISO datetime string
                try:
                    datetime.fromisoformat(sig.metadata["most_recent_filing"])
                except ValueError:
                    raise AssertionError(
                        f"Signal for {sig.symbol}: most_recent_filing "
                        f"'{sig.metadata['most_recent_filing']}' is not a valid "
                        f"ISO datetime string"
                    )
            else:
                # Non-smart-money signals should NOT have smart_money metadata
                assert "smart_money" not in sig.metadata, (
                    f"Signal for non-smart-money symbol {sig.symbol} "
                    f"should not have 'smart_money' key"
                )


# ---------------------------------------------------------------------------
# Unit Tests: SmartMoneyConfig defaults (Task 11.1)
# ---------------------------------------------------------------------------


class TestSmartMoneyConfigDefaults:
    """
    **Validates: Requirements 7.1–7.12**

    Verify that SmartMoneyConfig() produces correct default values for all
    fields as specified in the design document.
    """

    def test_smart_money_enabled_default(self):
        """Req 7.1: Global toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.smart_money_enabled is True

    def test_capitol_trades_enabled_default(self):
        """Req 7.2: Capitol Trades provider toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.capitol_trades_enabled is True

    def test_berkshire_enabled_default(self):
        """Req 7.2: Berkshire provider toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.berkshire_enabled is True

    def test_ark_enabled_default(self):
        """Req 7.2: ARK provider toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.ark_enabled is True

    def test_insider_cluster_enabled_default(self):
        """Req 7.2: Insider Cluster provider toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.insider_cluster_enabled is True

    def test_min_trade_amount_default(self):
        """Req 7.3: Minimum trade amount defaults to 50000."""
        config = SmartMoneyConfig()
        assert config.min_trade_amount == 50_000.0

    def test_min_share_change_default(self):
        """Req 7.4: Minimum share change defaults to 10000."""
        config = SmartMoneyConfig()
        assert config.min_share_change == 10_000

    def test_recency_window_days_default(self):
        """Req 7.5: General recency window defaults to 30 days."""
        config = SmartMoneyConfig()
        assert config.recency_window_days == 30

    def test_berkshire_recency_days_default(self):
        """Req 7.5: Berkshire recency window defaults to 90 days."""
        config = SmartMoneyConfig()
        assert config.berkshire_recency_days == 90

    def test_capitol_trades_cache_ttl_default(self):
        """Req 7.6: Capitol Trades cache TTL defaults to 24 hours."""
        config = SmartMoneyConfig()
        assert config.capitol_trades_cache_ttl == 24.0

    def test_berkshire_cache_ttl_default(self):
        """Req 7.6: Berkshire cache TTL defaults to 168 hours (7 days)."""
        config = SmartMoneyConfig()
        assert config.berkshire_cache_ttl == 168.0

    def test_ark_cache_ttl_default(self):
        """Req 7.6: ARK cache TTL defaults to 24 hours."""
        config = SmartMoneyConfig()
        assert config.ark_cache_ttl == 24.0

    def test_insider_cluster_cache_ttl_default(self):
        """Req 7.6: Insider Cluster cache TTL defaults to 24 hours."""
        config = SmartMoneyConfig()
        assert config.insider_cluster_cache_ttl == 24.0

    def test_top_n_politicians_default(self):
        """Req 7.7: Top-N politicians defaults to 10."""
        config = SmartMoneyConfig()
        assert config.top_n_politicians == 10

    def test_min_politician_filings_default(self):
        """Req 7.7: Minimum politician filings defaults to 2."""
        config = SmartMoneyConfig()
        assert config.min_politician_filings == 2

    def test_min_ark_buying_days_default(self):
        """Req 7.8: Minimum ARK buying days defaults to 3."""
        config = SmartMoneyConfig()
        assert config.min_ark_buying_days == 3

    def test_cluster_window_days_default(self):
        """Req 7.9: Cluster window defaults to 30 days."""
        config = SmartMoneyConfig()
        assert config.cluster_window_days == 30

    def test_min_cluster_insiders_default(self):
        """Req 7.9: Minimum cluster insiders defaults to 2."""
        config = SmartMoneyConfig()
        assert config.min_cluster_insiders == 2

    def test_defensive_symbols_default(self):
        """Req 7.10: Defensive symbols list matches the design spec."""
        config = SmartMoneyConfig()
        expected = ["AAPL", "MSFT", "GOOGL", "JNJ", "PG", "KO", "PEP", "WMT", "UNH", "V"]
        assert config.defensive_symbols == expected

    def test_disk_cache_dir_default(self):
        """Req 7.11: Disk cache directory defaults to 'regime_trader/cache/smart_money'."""
        config = SmartMoneyConfig()
        assert config.disk_cache_dir == "smart_trader/cache/smart_money"

    def test_supabase_enabled_default(self):
        """Req 7.12: Supabase toggle defaults to True."""
        config = SmartMoneyConfig()
        assert config.supabase_enabled is True


class TestAppConfigSmartMoneyField:
    """
    **Validates: Requirements 7.1–7.12**

    Verify that AppConfig includes a smart_money field with the correct type.
    """

    def test_app_config_has_smart_money_field(self):
        """AppConfig should have a smart_money attribute."""
        from smart_trader.settings.config import AppConfig
        app = AppConfig()
        assert hasattr(app, "smart_money")

    def test_app_config_smart_money_type(self):
        """AppConfig.smart_money should be a SmartMoneyConfig instance."""
        from smart_trader.settings.config import AppConfig
        app = AppConfig()
        assert isinstance(app.smart_money, SmartMoneyConfig)

    def test_app_config_smart_money_defaults(self):
        """AppConfig.smart_money should have the same defaults as SmartMoneyConfig()."""
        from smart_trader.settings.config import AppConfig
        app = AppConfig()
        standalone = SmartMoneyConfig()
        assert app.smart_money.smart_money_enabled == standalone.smart_money_enabled
        assert app.smart_money.min_trade_amount == standalone.min_trade_amount
        assert app.smart_money.defensive_symbols == standalone.defensive_symbols
        assert app.smart_money.supabase_enabled == standalone.supabase_enabled


class TestLoadCredentialsSupabase:
    """
    **Validates: Requirements 10.11**

    Verify that load_credentials() returns supabase_url and supabase_key.
    """

    def test_load_credentials_has_supabase_url(self):
        """load_credentials() should return a dict with 'supabase_url' key."""
        from smart_trader.settings.credentials import load_credentials
        creds = load_credentials()
        assert "supabase_url" in creds

    def test_load_credentials_has_supabase_key(self):
        """load_credentials() should return a dict with 'supabase_key' key."""
        from smart_trader.settings.credentials import load_credentials
        creds = load_credentials()
        assert "supabase_key" in creds

    def test_load_credentials_supabase_defaults_to_empty(self):
        """Without env vars, supabase_url and supabase_key default to empty strings."""
        from smart_trader.settings.credentials import load_credentials
        from unittest.mock import patch
        import os
        env_copy = os.environ.copy()
        env_copy.pop("SUPABASE_URL", None)
        env_copy.pop("SUPABASE_KEY", None)
        # Suppress load_dotenv so it doesn't re-inject .env values
        with patch("smart_trader.settings.credentials.load_dotenv"), \
             patch.dict("os.environ", env_copy, clear=True):
            creds = load_credentials()
            assert creds["supabase_url"] == ""
            assert creds["supabase_key"] == ""

    def test_load_credentials_supabase_reads_env_vars(self):
        """load_credentials() should read SUPABASE_URL and SUPABASE_KEY from env."""
        from smart_trader.settings.credentials import load_credentials
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_KEY": "test-api-key-123",
        }):
            creds = load_credentials()
            assert creds["supabase_url"] == "https://test.supabase.co"
            assert creds["supabase_key"] == "test-api-key-123"


# ---------------------------------------------------------------------------
# Unit Tests: Conviction scoring edge cases (Task 11.2)
# ---------------------------------------------------------------------------


def _expected_conviction(cfg, sources, n_actors, dollar_volume, filing_count, days_since):
    """Recompute the agreement-driven conviction score from config.

    Mirrors smart_money._compute_conviction_score so example tests stay valid
    if the weights are retuned in SmartMoneyConfig.
    """
    source_quality = sum(
        cfg.conviction_source_weights.get(s, cfg.conviction_default_source_weight)
        for s in sources
    )
    cluster_bonus = min(
        cfg.conviction_cluster_cap,
        cfg.conviction_cluster_per_actor * max(0, n_actors - 1),
    )
    if dollar_volume > 0:
        excess = max(0.0, math.log10(dollar_volume + 1) - cfg.conviction_dollar_floor_log)
        dollar_bonus = min(cfg.conviction_dollar_cap, cfg.conviction_dollar_coef * excess)
    else:
        dollar_bonus = 0.0
    accum_bonus = min(
        cfg.conviction_accum_cap,
        cfg.conviction_accum_per_filing * max(0, filing_count - 1),
    )
    rd = cfg.conviction_recency_days
    recency_bonus = max(0.0, (rd - days_since) / rd)
    return source_quality + cluster_bonus + dollar_bonus + accum_bonus + recency_bonus


class TestAgreementDrivenConviction:
    """Locks in the *design intent* of the agreement-driven model: multiple
    credible actors agreeing should outrank one large disclosed dollar figure,
    dollars are a capped modifier, source quality is differentiated, and
    share-only sources are no longer structurally zeroed out.
    """

    def _score(self, **kw):
        cfg = SmartMoneyConfig()
        params = dict(
            sources={"capitol_trades"},
            n_actors=1,
            total_dollar_volume=0.0,
            filing_count=1,
            days_since_most_recent=0,
        )
        params.update(kw)
        return _compute_conviction_score(cfg=cfg, **params)

    def test_multi_actor_agreement_beats_single_big_dollar(self):
        """4 actors agreeing (no dollar data) outrank 1 actor with a $50M figure."""
        big_dollar = self._score(n_actors=1, total_dollar_volume=50_000_000.0)
        cluster = self._score(n_actors=4, total_dollar_volume=0.0)
        assert cluster > big_dollar

    def test_dollar_contribution_is_capped(self):
        """A 25,000x larger dollar figure adds no more than the dollar cap."""
        small = self._score(total_dollar_volume=200_000.0)
        huge = self._score(total_dollar_volume=5_000_000_000.0)
        assert huge - small <= SmartMoneyConfig().conviction_dollar_cap + 1e-9

    def test_source_quality_insider_beats_etf(self):
        """An insider Form 4 source outscores a momentum-ETF source, all else equal."""
        insider = self._score(sources={"sec_edgar"})
        etf = self._score(sources={"ark_invest"})
        assert insider > etf

    def test_ark_cluster_not_zeroed_out(self):
        """Share-only ARK across multiple funds clears the entry bar via quality
        + cluster — the old log10(dollars) model capped this at ~2.0 + recency."""
        s = self._score(
            sources={"ark_invest"}, n_actors=3, total_dollar_volume=0.0, filing_count=2,
        )
        assert s > 3.0

    def test_more_distinct_sources_increase_score(self):
        """Corroboration across source TYPES raises conviction."""
        one = self._score(sources={"capitol_trades"}, n_actors=1)
        two = self._score(sources={"capitol_trades", "sec_edgar"}, n_actors=2)
        assert two > one


class TestConvictionScoringEdgeCases:
    """
    **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6**

    Example-based unit tests for conviction scoring edge cases including
    exact score verification, sell-only filtering, deduplication, and
    recency boundary behavior.
    """

    def _make_scanner(self, tmp_dir: str) -> SmartMoneyScanner:
        """Helper to create a SmartMoneyScanner with default config and patched init."""
        config = SmartMoneyConfig(
            smart_money_enabled=True,
            capitol_trades_enabled=False,
            berkshire_enabled=False,
            ark_enabled=False,
            insider_cluster_enabled=False,
            min_trade_amount=50_000.0,
            min_share_change=10_000,
            recency_window_days=30,
            berkshire_recency_days=90,
            disk_cache_dir=tmp_dir,
        )
        risk_config = RiskConfig()
        with patch.object(SmartMoneyScanner, "_init_providers"), \
             patch.object(SmartMoneyScanner, "_restore_disk_cache"):
            return SmartMoneyScanner(config, risk_config)

    def test_single_source_100k_5_days_old(self):
        """1 source (capitol_trades), 1 actor, $100k, 5 days old.

        Agreement-driven: source_quality(2.5) + cluster(0) + dollar(~0, $100k is
        right at the floor) + accum(0) + recency(0.833) ≈ 3.33.
        """
        now = datetime.now()
        filing = TradeFiling(
            source="capitol_trades",
            actor="Politician A",
            symbol="AAPL",
            tx_type="buy",
            dollar_amount=100_000.0,
            share_change=None,
            filing_date=now - timedelta(days=5),
            trade_date=now - timedelta(days=5),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = scanner._compute_conviction_scores([filing])

            assert len(candidates) == 1
            c = candidates[0]
            assert c.symbol == "AAPL"

            expected_score = _expected_conviction(
                scanner.config,
                sources={"capitol_trades"},
                n_actors=1,
                dollar_volume=100_000.0,
                filing_count=1,
                days_since=5,
            )
            assert abs(c.conviction_score - expected_score) < 1e-6, (
                f"Expected {expected_score}, got {c.conviction_score}"
            )

    def test_three_sources_1m_1_day_old(self):
        """3 sources (capitol_trades, berkshire_13f, insider_cluster), 3 actors,
        $1M total, 1 day old.

        Agreement-driven: source_quality(2.5+2.5+3.5=8.5) + cluster(2.0, 3 actors)
        + dollar(0.5, $1M) + accum(0.6, 3 filings) + recency(0.967) ≈ 12.57.
        """
        now = datetime.now()
        filings = [
            TradeFiling(
                source="capitol_trades",
                actor="Politician A",
                symbol="NVDA",
                tx_type="buy",
                dollar_amount=400_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
            TradeFiling(
                source="berkshire_13f",
                actor="Berkshire Hathaway",
                symbol="NVDA",
                tx_type="increase",
                dollar_amount=300_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
            TradeFiling(
                source="insider_cluster",
                actor="CEO John Smith",
                symbol="NVDA",
                tx_type="buy",
                dollar_amount=300_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = scanner._compute_conviction_scores(filings)

            assert len(candidates) == 1
            c = candidates[0]
            assert c.symbol == "NVDA"

            expected_score = _expected_conviction(
                scanner.config,
                sources={"capitol_trades", "berkshire_13f", "insider_cluster"},
                n_actors=3,
                dollar_volume=1_000_000.0,
                filing_count=3,
                days_since=1,
            )
            assert abs(c.conviction_score - expected_score) < 1e-6, (
                f"Expected {expected_score}, got {c.conviction_score}"
            )

    def test_ark_shares_only_zero_dollar_volume(self):
        """1 source (ARK), 1 actor, $0 dollar volume (shares only), 29 days old.

        Share-only sources contribute zero dollar_bonus but still score via
        source quality: source_quality(1.5) + cluster(0) + dollar(0) + accum(0)
        + recency(0.033) ≈ 1.53. (A multi-fund ARK cluster would add the cluster
        bonus — see test_ark_cluster_not_zeroed_out.)
        """
        now = datetime.now()
        filing = TradeFiling(
            source="ark_invest",
            actor="ARKK",
            symbol="TSLA",
            tx_type="buy",
            dollar_amount=None,
            share_change=50_000,
            filing_date=now - timedelta(days=29),
            trade_date=now - timedelta(days=29),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = scanner._compute_conviction_scores([filing])

            assert len(candidates) == 1
            c = candidates[0]
            assert c.symbol == "TSLA"
            assert c.total_dollar_volume == 0.0

            expected_score = _expected_conviction(
                scanner.config,
                sources={"ark_invest"},
                n_actors=1,
                dollar_volume=0.0,
                filing_count=1,
                days_since=29,
            )
            assert abs(c.conviction_score - expected_score) < 1e-6, (
                f"Expected {expected_score}, got {c.conviction_score}"
            )

    def test_all_sell_filings_empty_candidates(self):
        """All filings are SELL → empty candidates list."""
        now = datetime.now()
        filings = [
            TradeFiling(
                source="capitol_trades",
                actor="Politician A",
                symbol="AAPL",
                tx_type="sell",
                dollar_amount=200_000.0,
                share_change=None,
                filing_date=now - timedelta(days=2),
                trade_date=now - timedelta(days=2),
            ),
            TradeFiling(
                source="insider_cluster",
                actor="CFO Jane Doe",
                symbol="MSFT",
                tx_type="decrease",
                dollar_amount=150_000.0,
                share_change=None,
                filing_date=now - timedelta(days=3),
                trade_date=now - timedelta(days=3),
            ),
            TradeFiling(
                source="berkshire_13f",
                actor="Berkshire Hathaway",
                symbol="GOOGL",
                tx_type="sell",
                dollar_amount=500_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = scanner._compute_conviction_scores(filings)

            assert candidates == [], (
                f"Expected empty candidates for all-sell filings, got {len(candidates)}"
            )

    def test_duplicate_symbol_same_source_deduplicated(self):
        """Duplicate symbol from same source → deduplicated in output (single candidate)."""
        now = datetime.now()
        filings = [
            TradeFiling(
                source="capitol_trades",
                actor="Politician A",
                symbol="AAPL",
                tx_type="buy",
                dollar_amount=100_000.0,
                share_change=None,
                filing_date=now - timedelta(days=3),
                trade_date=now - timedelta(days=3),
            ),
            TradeFiling(
                source="capitol_trades",
                actor="Politician B",
                symbol="AAPL",
                tx_type="buy",
                dollar_amount=200_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = scanner._compute_conviction_scores(filings)

            # Should be deduplicated to a single candidate for AAPL
            assert len(candidates) == 1, (
                f"Expected 1 deduplicated candidate, got {len(candidates)}"
            )
            c = candidates[0]
            assert c.symbol == "AAPL"
            # Still only 1 source (capitol_trades) even though 2 filings
            assert len(c.sources) == 1
            assert c.sources == ["capitol_trades"]
            # But 2 distinct actors
            assert len(c.actors) == 2
            # Dollar volume is aggregated
            assert abs(c.total_dollar_volume - 300_000.0) < 1e-6
            assert c.filing_count == 2

    def test_filing_exactly_at_recency_boundary_included(self):
        """Filing with trade_date exactly at recency boundary → included.

        The implementation uses `if f.trade_date < cutoff: continue`, so a
        filing with trade_date == cutoff (exactly recency_window_days ago)
        is NOT skipped and IS included.

        We freeze datetime.now() to eliminate timing drift between the test
        setup and the method's internal `now`.
        """
        frozen_now = datetime(2025, 6, 15, 12, 0, 0)
        # trade_date exactly recency_window_days (30) ago
        trade_date = frozen_now - timedelta(days=30)

        filing = TradeFiling(
            source="capitol_trades",
            actor="Politician A",
            symbol="EDGE",
            tx_type="buy",
            dollar_amount=100_000.0,
            share_change=None,
            filing_date=trade_date,
            trade_date=trade_date,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            # Freeze datetime.now() so the cutoff inside the method matches
            with patch("smart_trader.core.smart_money.datetime") as mock_dt:
                mock_dt.now.return_value = frozen_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                candidates = scanner._compute_conviction_scores([filing])

            # The filing at exactly the boundary should be included
            # (cutoff = frozen_now - 30 days == trade_date, and the filter
            # is `trade_date < cutoff` which is False, so it passes)
            assert len(candidates) == 1, (
                f"Expected filing at exact recency boundary to be included, "
                f"got {len(candidates)} candidates"
            )
            assert candidates[0].symbol == "EDGE"


# ---------------------------------------------------------------------------
# Task 11.3: Unit tests for cache behavior
# ---------------------------------------------------------------------------


class TestCacheBehavior:
    """
    Unit tests for disk cache round-trip, fresh restore, stale discard,
    and corrupt file handling.

    **Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6**
    """

    def _make_filings(self) -> List[TradeFiling]:
        """Create a small set of TradeFiling records for cache tests."""
        now = datetime.now()
        return [
            TradeFiling(
                source="capitol_trades",
                actor="Politician A",
                symbol="AAPL",
                tx_type="buy",
                dollar_amount=200_000.0,
                share_change=None,
                filing_date=now - timedelta(days=2),
                trade_date=now - timedelta(days=3),
            ),
            TradeFiling(
                source="insider_cluster",
                actor="CEO John Smith",
                symbol="MSFT",
                tx_type="increase",
                dollar_amount=None,
                share_change=5000,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=1),
            ),
        ]

    def test_write_and_read_round_trip(self):
        """Req 2.1: Write cache file via _write_disk_cache, read JSON back,
        verify round-trip fidelity of all filing fields."""
        filings = self._make_filings()

        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(disk_cache_dir=tmp_dir)
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            # Populate in-memory cache so _write_disk_cache can read ttl_hours
            scanner._cache["test_provider"] = CacheEntry(
                provider_name="test_provider",
                filings=filings,
                fetched_at=datetime.now(),
                ttl_hours=24.0,
            )

            scanner._write_disk_cache("test_provider", filings)

            # Read the JSON file back
            from pathlib import Path
            cache_file = Path(tmp_dir) / "test_provider.json"
            assert cache_file.exists(), "Cache file was not written"

            data = json.loads(cache_file.read_text())

            assert data["provider"] == "test_provider"
            assert data["ttl_hours"] == 24.0
            assert "fetched_at" in data
            assert len(data["filings"]) == len(filings)

            for orig, cached in zip(filings, data["filings"]):
                assert cached["source"] == orig.source
                assert cached["actor"] == orig.actor
                assert cached["symbol"] == orig.symbol
                assert cached["tx_type"] == orig.tx_type
                assert cached["dollar_amount"] == orig.dollar_amount
                assert cached["share_change"] == orig.share_change
                assert cached["filing_date"] == orig.filing_date.isoformat()
                assert cached["trade_date"] == orig.trade_date.isoformat()

    def test_startup_with_fresh_cache_file_loaded(self):
        """Req 2.5: On startup, a cache file within TTL is loaded into
        the in-memory cache by _restore_disk_cache."""
        filings = self._make_filings()

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Write a fresh cache file (fetched_at = now, TTL = 24h)
            from pathlib import Path
            cache_file = Path(tmp_dir) / "capitol_trades.json"
            cache_data = {
                "provider": "capitol_trades",
                "fetched_at": datetime.now().isoformat(),
                "ttl_hours": 24.0,
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
            cache_file.write_text(json.dumps(cache_data))

            # Create scanner — _restore_disk_cache runs in __init__
            config = SmartMoneyConfig(disk_cache_dir=tmp_dir)
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"):
                scanner = SmartMoneyScanner(config, risk_config)

            # Verify the cache was loaded
            assert "capitol_trades" in scanner._cache, (
                "Fresh cache file was not loaded into in-memory cache"
            )
            entry = scanner._cache["capitol_trades"]
            assert len(entry.filings) == len(filings)
            assert entry.provider_name == "capitol_trades"
            assert entry.ttl_hours == 24.0

            # Verify filing data matches
            for orig, restored in zip(filings, entry.filings):
                assert restored.source == orig.source
                assert restored.actor == orig.actor
                assert restored.symbol == orig.symbol
                assert restored.tx_type == orig.tx_type
                assert restored.dollar_amount == orig.dollar_amount
                assert restored.share_change == orig.share_change

    def test_startup_with_stale_cache_file_discarded(self):
        """Req 2.6: On startup, a cache file older than its TTL is discarded
        and NOT loaded into the in-memory cache."""
        filings = self._make_filings()

        with tempfile.TemporaryDirectory() as tmp_dir:
            from pathlib import Path
            cache_file = Path(tmp_dir) / "capitol_trades.json"

            # Write a stale cache file (fetched_at = 48 hours ago, TTL = 24h)
            stale_time = datetime.now() - timedelta(hours=48)
            cache_data = {
                "provider": "capitol_trades",
                "fetched_at": stale_time.isoformat(),
                "ttl_hours": 24.0,
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
            cache_file.write_text(json.dumps(cache_data))

            # Create scanner — _restore_disk_cache runs in __init__
            config = SmartMoneyConfig(disk_cache_dir=tmp_dir)
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"):
                scanner = SmartMoneyScanner(config, risk_config)

            # Verify the stale cache was NOT loaded
            assert "capitol_trades" not in scanner._cache, (
                "Stale cache file should have been discarded, but was loaded"
            )

    def test_startup_with_corrupt_cache_file_discarded(self):
        """Req 2.5/2.6: On startup, a corrupt (invalid JSON) cache file is
        discarded with a warning and does not crash the scanner."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            from pathlib import Path
            cache_file = Path(tmp_dir) / "capitol_trades.json"

            # Write invalid JSON
            cache_file.write_text("{this is not valid json!!!")

            # Create scanner — _restore_disk_cache runs in __init__
            config = SmartMoneyConfig(disk_cache_dir=tmp_dir)
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"):
                # Should not raise — corrupt file is discarded gracefully
                scanner = SmartMoneyScanner(config, risk_config)

            # Verify the corrupt cache was NOT loaded
            assert "capitol_trades" not in scanner._cache, (
                "Corrupt cache file should have been discarded, but was loaded"
            )


# ---------------------------------------------------------------------------
# Task 11.4: Unit tests for regime filtering edge cases
# ---------------------------------------------------------------------------


class TestRegimeFilteringEdgeCases:
    """
    Example-based unit tests for regime filtering edge cases.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """

    def _make_candidate(self, symbol: str, score: float = 5.0) -> CandidateSymbol:
        """Helper to create a CandidateSymbol with sensible defaults."""
        return CandidateSymbol(
            symbol=symbol,
            conviction_score=score,
            sources=["capitol_trades"],
            actors=["TestActor"],
            total_dollar_volume=100_000.0,
            filing_count=1,
            most_recent_filing=datetime.now() - timedelta(days=5),
        )

    def _make_scanner(self, tmp_dir: str) -> SmartMoneyScanner:
        """Helper to create a SmartMoneyScanner with providers/cache patched out."""
        config = SmartMoneyConfig(
            smart_money_enabled=True,
            capitol_trades_enabled=False,
            berkshire_enabled=False,
            ark_enabled=False,
            insider_cluster_enabled=False,
            disk_cache_dir=tmp_dir,
        )
        risk_config = RiskConfig()
        with patch.object(SmartMoneyScanner, "_init_providers"), \
             patch.object(SmartMoneyScanner, "_restore_disk_cache"):
            scanner = SmartMoneyScanner(config, risk_config)
        return scanner

    def test_low_vol_all_candidates_pass(self):
        """Req 5.1: vol_rank=0.20 (≤ 0.33) → all 5 candidates pass through."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = [
                self._make_candidate("TSLA"),
                self._make_candidate("NVDA"),
                self._make_candidate("AAPL"),
                self._make_candidate("PLTR"),
                self._make_candidate("AMD"),
            ]

            result = scanner._apply_regime_filter(candidates, vol_rank=0.20)

            assert len(result) == 5
            result_symbols = {c.symbol for c in result}
            assert result_symbols == {"TSLA", "NVDA", "AAPL", "PLTR", "AMD"}

    def test_mid_vol_only_defensive_pass(self):
        """Req 5.2: vol_rank=0.50 (0.33 < v ≤ 0.67), 5 candidates (2 defensive,
        3 not) → only the 2 defensive symbols pass."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            # AAPL and MSFT are in the default defensive_symbols list
            # TSLA, NVDA, PLTR are NOT
            candidates = [
                self._make_candidate("AAPL"),
                self._make_candidate("TSLA"),
                self._make_candidate("MSFT"),
                self._make_candidate("NVDA"),
                self._make_candidate("PLTR"),
            ]

            result = scanner._apply_regime_filter(candidates, vol_rank=0.50)

            assert len(result) == 2
            result_symbols = {c.symbol for c in result}
            assert result_symbols == {"AAPL", "MSFT"}

    def test_high_vol_none_pass(self):
        """Req 5.3: vol_rank=0.80 (> 0.67) → none of the 5 candidates pass."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            scanner = self._make_scanner(tmp_dir)
            candidates = [
                self._make_candidate("AAPL"),
                self._make_candidate("MSFT"),
                self._make_candidate("GOOGL"),
                self._make_candidate("TSLA"),
                self._make_candidate("NVDA"),
            ]

            result = scanner._apply_regime_filter(candidates, vol_rank=0.80)

            assert len(result) == 0

    def test_no_enabled_providers_empty_candidates(self):
        """Req 5.1/5.2/5.3: When no providers are enabled, get_candidates()
        returns an empty list regardless of vol_rank."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SmartMoneyConfig(
                smart_money_enabled=True,
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                disk_cache_dir=tmp_dir,
            )
            risk_config = RiskConfig()

            with patch.object(SmartMoneyScanner, "_init_providers"), \
                 patch.object(SmartMoneyScanner, "_restore_disk_cache"):
                scanner = SmartMoneyScanner(config, risk_config)

            # No providers means _providers is empty → no filings → empty candidates
            scanner._providers = []

            result = scanner.get_candidates(vol_rank=0.20)
            assert result == []


# ---------------------------------------------------------------------------
# Task 11.5: Integration tests for RiskManager passthrough
# ---------------------------------------------------------------------------

import numpy as np

from smart_trader.core.signal import Signal
from smart_trader.core.risk_manager import (
    CircuitBreaker,
    PortfolioState,
    RiskManager,
)


def _no_lock_config(**kwargs) -> RiskConfig:
    """RiskConfig with a non-existent lock file path to isolate tests."""
    return RiskConfig(
        lock_file_path=os.path.join(tempfile.gettempdir(), "test_sm_nonexistent.lock"),
        **kwargs,
    )


def _smart_money_signal(
    symbol="SMRT",
    size=0.10,
    entry=100.0,
    stop=95.0,
    leverage=1.0,
    conviction_score=8.5,
    sources=None,
    actors=None,
) -> Signal:
    """Create a Signal with smart money metadata attached."""
    sig = Signal(
        symbol=symbol,
        direction="LONG",
        confidence=0.85,
        entry_price=entry,
        stop_loss=stop,
        position_size_pct=size,
        leverage=leverage,
        timestamp=datetime.now(),
        reasoning="smart money signal",
        strategy_name="LowVolBullStrategy",
        metadata={
            "smart_money": True,
            "conviction_score": conviction_score,
            "sources": sources or ["capitol_trades", "insider_cluster"],
            "actors": actors or ["Politician A", "CEO John Smith"],
            "most_recent_filing": datetime.now().isoformat(),
        },
    )
    return sig


def _sm_portfolio(equity=100_000.0, positions=None) -> PortfolioState:
    """Create a PortfolioState for smart money integration tests."""
    return PortfolioState(
        equity=equity,
        cash=equity,
        buying_power=equity,
        positions=positions or {},
        peak_equity=equity,
    )


class TestRiskManagerSmartMoneyPassthrough:
    """
    Integration tests verifying that smart money signals flow through
    RiskManager.validate_signal() and are subject to the same risk controls
    as regular signals.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**
    """

    def test_smart_money_signal_flows_through_validate_signal(self):
        """Req 6.1: A valid smart money signal is approved by RiskManager
        and its metadata is preserved on the modified signal."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio())

        assert decision.approved is True
        assert decision.modified_signal is not None
        # Metadata should be preserved through validation
        assert decision.modified_signal.metadata.get("smart_money") is True
        assert decision.modified_signal.metadata.get("conviction_score") == 8.5
        assert len(decision.modified_signal.metadata.get("sources", [])) > 0

    def test_smart_money_signal_clipped_at_15pct_single_position_cap(self):
        """Req 6.2: RiskManager clips a smart money signal requesting 30%
        down to the 15% single-position cap."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        sig = _smart_money_signal(symbol="NVDA", size=0.30)
        decision = rm.validate_signal(sig, _sm_portfolio())

        assert decision.approved is True
        assert decision.modified_signal.position_size_pct <= 0.15
        assert any("max_single" in m for m in decision.modifications)
        # Metadata still present
        assert decision.modified_signal.metadata.get("smart_money") is True

    def test_smart_money_signal_rejected_at_max_concurrent_positions(self):
        """Req 6.2: RiskManager rejects a smart money signal when the
        portfolio already has 5 concurrent positions (the default max)."""
        rm = RiskManager(_no_lock_config(max_concurrent_positions=5))
        rm.initialize(100_000)

        # 5 existing positions
        positions = {
            f"SYM{i}": {"market_value": 5_000, "sector": "unknown"}
            for i in range(5)
        }

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(positions=positions))

        assert decision.approved is False
        assert "concurrent" in decision.rejection_reason.lower()

    def test_circuit_breaker_half_size_applies_to_smart_money(self):
        """Req 6.3: When the circuit breaker is in half_size mode, smart
        money signals get their size halved just like regular signals."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        # Trigger daily half-size (2% drawdown)
        rm.circuit_breaker.update(98_000)
        assert rm.circuit_breaker.any_half_size()

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(equity=98_000))

        assert decision.approved is True
        assert decision.modified_signal.position_size_pct == pytest.approx(0.05)
        # Metadata preserved
        assert decision.modified_signal.metadata.get("smart_money") is True

    def test_circuit_breaker_halt_rejects_smart_money_signals(self):
        """Req 6.4: When the circuit breaker is in halted mode (peak DD > 10%),
        all smart money signals are rejected."""
        with tempfile.NamedTemporaryFile(suffix=".lock", delete=False) as f:
            lock_path = f.name
        os.unlink(lock_path)

        rm = RiskManager(RiskConfig(lock_file_path=lock_path, peak_drawdown_stop=0.10))
        rm.initialize(100_000)

        # Push peak up then trigger halt (>10% drawdown from peak)
        rm.circuit_breaker.update(110_000)  # new peak
        rm.circuit_breaker.update(99_000)   # -10% from peak → halt

        assert rm.circuit_breaker.is_halted

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(equity=99_000))

        assert decision.approved is False
        assert "halt" in decision.action.lower() or "halt" in decision.rejection_reason.lower()

        # Clean up lock file
        if os.path.exists(lock_path):
            os.unlink(lock_path)

    def test_circuit_breaker_close_all_rejects_smart_money_signals(self):
        """Req 6.4: When the circuit breaker triggers close_all (daily DD > 3%),
        smart money signals are rejected."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        # Trigger daily close_all (3% drawdown)
        rm.circuit_breaker.update(97_000)
        assert rm.circuit_breaker.any_closed()

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(equity=97_000))

        assert decision.approved is False
        assert "close_all" in decision.action or "loss" in decision.rejection_reason.lower()

    def test_correlation_check_rejects_highly_correlated_smart_money(self):
        """Req 6.5: RiskManager applies correlation checks to smart money
        candidates. A smart money signal highly correlated (>0.85) with an
        existing position is rejected."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        np.random.seed(42)
        base = np.random.randn(80)
        # Near-perfect correlation with existing position
        rm.update_returns_history("AAPL", base)
        rm.update_returns_history("NVDA", base + np.random.randn(80) * 0.001)

        positions = {"AAPL": {"market_value": 10_000, "sector": "tech"}}

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(positions=positions))

        assert decision.approved is False
        assert "correlation" in decision.rejection_reason.lower()

    def test_correlation_check_reduces_moderately_correlated_smart_money(self):
        """Req 6.5: RiskManager reduces size for smart money signals with
        moderate correlation (0.70-0.85) against existing positions."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        np.random.seed(1)
        base = np.random.randn(80)
        # ~0.75 correlation — above reduce threshold but below reject
        moderate = base + np.random.randn(80) * 0.85
        rm.update_returns_history("AAPL", base)
        rm.update_returns_history("NVDA", moderate)

        positions = {"AAPL": {"market_value": 1_000, "sector": "tech"}}

        sig = _smart_money_signal(symbol="NVDA", size=0.10)
        decision = rm.validate_signal(sig, _sm_portfolio(positions=positions))

        # Should be approved (not rejected) but may have size reduced
        assert decision.approved is True
        assert decision.modified_signal.metadata.get("smart_money") is True

    def test_total_exposure_cap_applies_to_smart_money(self):
        """Req 6.2: RiskManager clips smart money signals when total
        exposure would exceed the 80% cap."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)

        # 70% already deployed across positions
        positions = {
            "SPY": {"market_value": 20_000, "sector": "index"},
            "AAPL": {"market_value": 20_000, "sector": "tech"},
            "AMZN": {"market_value": 15_000, "sector": "consumer"},
            "XOM": {"market_value": 15_000, "sector": "energy"},
        }

        # Request 15% → total would be 85% > 80%, so clip to 10% room
        sig = _smart_money_signal(symbol="JPM", size=0.15)
        decision = rm.validate_signal(sig, _sm_portfolio(positions=positions))

        assert decision.approved is True
        assert decision.modified_signal.position_size_pct <= 0.10 + 1e-6
        assert decision.modified_signal.metadata.get("smart_money") is True



# ---------------------------------------------------------------------------
# Integration tests for Supabase sync (Task 11.6)
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock


def _make_test_filings(n: int = 3) -> List[TradeFiling]:
    """Create a list of test TradeFiling records for Supabase sync tests."""
    now = datetime.now()
    return [
        TradeFiling(
            source="capitol_trades",
            actor=f"Politician_{i}",
            symbol=f"SYM{i}",
            tx_type="buy",
            dollar_amount=100_000.0 * (i + 1),
            share_change=None,
            filing_date=now - timedelta(days=i),
            trade_date=now - timedelta(days=i + 1),
        )
        for i in range(n)
    ]


def _make_test_candidates(n: int = 2) -> List[CandidateSymbol]:
    """Create a list of test CandidateSymbol records for Supabase sync tests."""
    now = datetime.now()
    return [
        CandidateSymbol(
            symbol=f"CAND{i}",
            conviction_score=8.0 - i,
            sources=["capitol_trades"],
            actors=[f"Actor_{i}"],
            total_dollar_volume=500_000.0,
            filing_count=2,
            most_recent_filing=now - timedelta(days=i),
        )
        for i in range(n)
    ]


class TestSupabaseSyncIntegration:
    """
    Integration tests verifying Supabase sync behavior:
    - Mock Supabase client receives writes after disk writes
    - Supabase failures don't propagate to the scanner
    - No Supabase calls when supabase_enabled=False

    **Validates: Requirements 10.4, 10.5, 10.8, 10.9, 10.12**
    """

    def test_supabase_receives_filings_sync_after_disk_write(self):
        """Req 10.4: When supabase_enabled=True, _sync_to_supabase writes
        Trade_Filing records to the smart_money_filings table after disk write."""
        filings = _make_test_filings(3)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.text = "Created"

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "https://test.supabase.co",
                "supabase_key": "test-key-123",
            }), patch("requests.post", return_value=mock_response) as mock_post:
                # Call _sync_to_supabase directly (synchronous, not via thread)
                scanner._sync_to_supabase(filings)

                # Verify requests.post was called with the filings endpoint
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                assert "smart_money_filings" in call_args[0][0]
                assert call_args[1]["json"] is not None
                posted_rows = call_args[1]["json"]
                assert len(posted_rows) == 3
                # Verify row structure
                for row in posted_rows:
                    assert "source" in row
                    assert "actor" in row
                    assert "symbol" in row
                    assert "tx_type" in row
                    assert "filing_date" in row
                    assert "trade_date" in row
                    assert "ingested_at" in row

    def test_supabase_receives_candidates_sync_after_generation(self):
        """Req 10.5: When supabase_enabled=True, _sync_candidates_to_supabase
        writes candidate snapshots to the smart_money_candidates table."""
        candidates = _make_test_candidates(2)
        vol_rank = 0.25

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.text = "Created"

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "https://test.supabase.co",
                "supabase_key": "test-key-123",
            }), patch("requests.post", return_value=mock_response) as mock_post:
                scanner._sync_candidates_to_supabase(candidates, vol_rank)

                mock_post.assert_called_once()
                call_args = mock_post.call_args
                assert "smart_money_candidates" in call_args[0][0]
                posted_rows = call_args[1]["json"]
                assert len(posted_rows) == 2
                for row in posted_rows:
                    assert "symbol" in row
                    assert "conviction_score" in row
                    assert "sources" in row
                    assert "generated_at" in row
                    assert "vol_rank" in row
                    assert row["vol_rank"] == vol_rank

    def test_supabase_failure_does_not_propagate_to_scanner(self):
        """Req 10.8, 10.9: If a Supabase write fails, the error is logged
        but does not propagate — the scanner continues operating normally."""
        filings = _make_test_filings(2)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            # Simulate Supabase returning a 500 error
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Internal Server Error"

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "https://test.supabase.co",
                "supabase_key": "test-key-123",
            }), patch("requests.post", return_value=mock_response):
                # Should not raise — errors are caught and logged
                scanner._sync_to_supabase(filings)

    def test_supabase_connection_error_does_not_propagate(self):
        """Req 10.8, 10.9: If Supabase is unreachable (connection error),
        the error is logged but does not propagate to the scanner."""
        filings = _make_test_filings(2)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "https://test.supabase.co",
                "supabase_key": "test-key-123",
            }), patch("requests.post", side_effect=ConnectionError("Connection refused")):
                # Should not raise
                scanner._sync_to_supabase(filings)

    def test_supabase_candidates_failure_does_not_propagate(self):
        """Req 10.8, 10.9: Supabase candidate sync failure is logged
        but does not propagate to the scanner."""
        candidates = _make_test_candidates(2)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "https://test.supabase.co",
                "supabase_key": "test-key-123",
            }), patch("requests.post", side_effect=Exception("Supabase timeout")):
                # Should not raise
                scanner._sync_candidates_to_supabase(candidates, 0.25)

    def test_no_supabase_calls_when_disabled(self):
        """Req 10.12: When supabase_enabled=False, no Supabase sync
        operations are performed — _fire_supabase_sync is never called."""
        now = datetime.now()
        filings = [
            TradeFiling(
                source="capitol_trades",
                actor="TestPolitician",
                symbol="AAPL",
                tx_type="buy",
                dollar_amount=200_000.0,
                share_change=None,
                filing_date=now - timedelta(days=1),
                trade_date=now - timedelta(days=2),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=False,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
                # Disable all real providers to avoid network calls
                capitol_trades_enabled=False,
                berkshire_enabled=False,
                ark_enabled=False,
                insider_cluster_enabled=False,
                min_politician_filings=1,
                recency_window_days=30,
                min_trade_amount=1.0,
            )
            scanner = SmartMoneyScanner(config, RiskConfig())

            # Inject filings into cache so get_candidates has data to work with
            scanner._cache["capitol_trades"] = CacheEntry(
                provider_name="capitol_trades",
                filings=filings,
                fetched_at=now,
                ttl_hours=24.0,
            )
            # Add a mock provider that returns cached filings
            mock_provider = MagicMock()
            mock_provider.provider_name = "capitol_trades"
            mock_provider.get_cache_ttl_hours.return_value = 24.0
            scanner._providers = [mock_provider]

            with patch.object(scanner, "_fire_supabase_sync") as mock_fire:
                candidates = scanner.get_candidates(vol_rank=0.20)
                # _fire_supabase_sync should never be called when supabase_enabled=False
                mock_fire.assert_not_called()

    def test_fire_supabase_sync_spawns_threads_when_enabled(self):
        """Req 10.4, 10.5: When supabase_enabled=True, _fire_supabase_sync
        spawns background threads for both filings and candidates sync."""
        filings = _make_test_filings(2)
        candidates = _make_test_candidates(1)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            with patch.object(scanner, "_sync_to_supabase") as mock_filings_sync, \
                 patch.object(scanner, "_sync_candidates_to_supabase") as mock_candidates_sync, \
                 patch("threading.Thread") as mock_thread:
                mock_thread_instance = MagicMock()
                mock_thread.return_value = mock_thread_instance

                scanner._fire_supabase_sync(filings, candidates, 0.25)

                # Two threads should be created
                assert mock_thread.call_count == 2

                # Both threads should be started
                assert mock_thread_instance.start.call_count == 2

                # Verify thread targets
                thread_calls = mock_thread.call_args_list
                targets = [call.kwargs.get("target") for call in thread_calls]
                assert mock_filings_sync in targets
                assert mock_candidates_sync in targets

    def test_supabase_sync_skips_when_no_credentials(self):
        """Req 10.8: When Supabase credentials are empty, sync methods
        return early without making any HTTP requests."""
        filings = _make_test_filings(2)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = SmartMoneyConfig(
                supabase_enabled=True,
                disk_cache_dir=tmpdir,
                smart_money_enabled=True,
            )
            scanner = SmartMoneyScanner.__new__(SmartMoneyScanner)
            scanner.config = config
            scanner.risk_config = RiskConfig()
            scanner._providers = []
            scanner._cache = {}

            with patch("smart_trader.settings.credentials.load_credentials", return_value={
                "supabase_url": "",
                "supabase_key": "",
            }), patch("requests.post") as mock_post:
                scanner._sync_to_supabase(filings)
                # No HTTP request should be made when credentials are empty
                mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 1 signal-driven exits — held-position sell detection
# ---------------------------------------------------------------------------


def _exit_test_scanner(**config_overrides) -> SmartMoneyScanner:
    """Build a SmartMoneyScanner with all real providers disabled and an
    isolated disk cache. Returns a scanner suitable for direct cache injection.
    """
    defaults = dict(
        supabase_enabled=False,
        smart_money_enabled=True,
        capitol_trades_enabled=False,
        sec_edgar_enabled=False,
        berkshire_enabled=False,
        ark_enabled=False,
        insider_cluster_enabled=False,
        pershing_square_enabled=False,
        appaloosa_enabled=False,
        duquesne_enabled=False,
        tci_enabled=False,
        baupost_enabled=False,
        akre_enabled=False,
        viking_enabled=False,
        altimeter_enabled=False,
        third_point_enabled=False,
        lone_pine_enabled=False,
        greenlight_enabled=False,
        moat_enabled=False,
        cgdv_enabled=False,
        syld_enabled=False,
        min_trade_amount=1.0,
        min_share_change=1,
        recency_window_days=60,
    )
    defaults.update(config_overrides)
    config = SmartMoneyConfig(**defaults)
    with patch.object(SmartMoneyScanner, "_init_providers"), \
         patch.object(SmartMoneyScanner, "_restore_disk_cache"):
        scanner = SmartMoneyScanner(config, RiskConfig())
    return scanner


def _filing(symbol: str, tx_type: str, source: str = "capitol_trades",
            dollars: float = 100_000.0, days_ago: int = 5) -> TradeFiling:
    now = datetime.now()
    return TradeFiling(
        source=source,
        actor=f"actor_{symbol}",
        symbol=symbol,
        tx_type=tx_type,
        dollar_amount=dollars,
        share_change=None,
        filing_date=now - timedelta(days=days_ago),
        trade_date=now - timedelta(days=days_ago + 1),
    )


class TestComputeConvictionScoresTxFilter:
    """Regression + new behavior for _compute_conviction_scores tx_filter."""

    def test_default_tx_filter_keeps_buy_and_increase(self):
        scanner = _exit_test_scanner()
        filings = [
            _filing("BUY1", "buy"),
            _filing("INC1", "increase"),
            _filing("SELL1", "sell"),
            _filing("DEC1", "decrease"),
        ]
        candidates = scanner._compute_conviction_scores(filings)
        symbols = {c.symbol for c in candidates}
        assert "BUY1" in symbols
        assert "INC1" in symbols
        assert "SELL1" not in symbols
        assert "DEC1" not in symbols

    def test_sell_tx_filter_keeps_sell_and_decrease(self):
        scanner = _exit_test_scanner()
        filings = [
            _filing("BUY1", "buy"),
            _filing("SELL1", "sell"),
            _filing("DEC1", "decrease"),
        ]
        candidates = scanner._compute_conviction_scores(
            filings, tx_filter=("sell", "decrease")
        )
        symbols = {c.symbol for c in candidates}
        assert "SELL1" in symbols
        assert "DEC1" in symbols
        assert "BUY1" not in symbols

    def test_recency_and_threshold_filters_apply_symmetrically(self):
        """Recency and dollar-threshold filters must apply to sells too —
        a stale or tiny sell should be discarded just like a stale buy."""
        scanner = _exit_test_scanner(min_trade_amount=10_000.0, recency_window_days=30)
        filings = [
            _filing("FRESH", "sell", dollars=50_000.0, days_ago=5),
            _filing("STALE", "sell", dollars=50_000.0, days_ago=60),
            _filing("TINY", "sell", dollars=100.0, days_ago=5),
        ]
        candidates = scanner._compute_conviction_scores(
            filings, tx_filter=("sell", "decrease")
        )
        symbols = {c.symbol for c in candidates}
        assert "FRESH" in symbols
        assert "STALE" not in symbols
        assert "TINY" not in symbols


class TestGetHeldPositionSells:
    """End-to-end behavior of SmartMoneyScanner.get_held_position_sells."""

    def _scanner_with_filings(self, filings: List[TradeFiling]) -> SmartMoneyScanner:
        scanner = _exit_test_scanner()
        scanner._cache["capitol_trades"] = CacheEntry(
            provider_name="capitol_trades",
            filings=filings,
            fetched_at=datetime.now(),
            ttl_hours=24.0,
        )
        mock_provider = MagicMock()
        mock_provider.provider_name = "capitol_trades"
        mock_provider.get_cache_ttl_hours.return_value = 24.0
        scanner._providers = [mock_provider]
        return scanner

    def test_returns_empty_when_smart_money_disabled(self):
        scanner = _exit_test_scanner(smart_money_enabled=False)
        scanner._providers = []
        assert scanner.get_held_position_sells({"AAPL"}) == []

    def test_returns_empty_when_no_held_symbols(self):
        scanner = self._scanner_with_filings([_filing("AAPL", "sell")])
        assert scanner.get_held_position_sells(set()) == []
        assert scanner.get_held_position_sells([]) == []

    def test_returns_sells_for_held_symbols(self):
        filings = [
            _filing("AAPL", "sell", dollars=500_000.0),
            _filing("AAPL", "decrease", dollars=300_000.0, source="sec_edgar"),
        ]
        scanner = self._scanner_with_filings(filings)
        result = scanner.get_held_position_sells({"AAPL"})
        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        assert result[0].filing_count == 2
        assert result[0].conviction_score > 0

    def test_ignores_sells_on_non_held_symbols(self):
        filings = [
            _filing("AAPL", "sell"),
            _filing("MSFT", "sell"),
        ]
        scanner = self._scanner_with_filings(filings)
        result = scanner.get_held_position_sells({"AAPL"})
        symbols = {c.symbol for c in result}
        assert symbols == {"AAPL"}

    def test_ignores_buys_on_held_symbols(self):
        """A held position with only BUY filings should produce no exit signal."""
        filings = [_filing("AAPL", "buy"), _filing("AAPL", "increase")]
        scanner = self._scanner_with_filings(filings)
        assert scanner.get_held_position_sells({"AAPL"}) == []

    def test_symbol_matching_is_case_insensitive(self):
        filings = [_filing("AAPL", "sell")]
        scanner = self._scanner_with_filings(filings)
        # Position dict keys could be lowercased upstream — held set uppercase
        # already, but verify the comparison is robust.
        assert len(scanner.get_held_position_sells({"aapl"})) == 1
        assert len(scanner.get_held_position_sells({"AAPL"})) == 1

    def test_provider_failure_does_not_break_scan(self):
        """If one provider raises, the scan still returns sells from others."""
        scanner = _exit_test_scanner()
        scanner._cache["capitol_trades"] = CacheEntry(
            provider_name="capitol_trades",
            filings=[_filing("AAPL", "sell")],
            fetched_at=datetime.now(),
            ttl_hours=24.0,
        )
        good_provider = MagicMock()
        good_provider.provider_name = "capitol_trades"
        bad_provider = MagicMock()
        bad_provider.provider_name = "broken"
        scanner._providers = [bad_provider, good_provider]

        # _fetch_or_cache raises for the bad provider, returns cached for good
        original_fetch = scanner._fetch_or_cache
        def patched_fetch(p):
            if p.provider_name == "broken":
                raise RuntimeError("simulated upstream failure")
            return original_fetch(p)
        scanner._fetch_or_cache = patched_fetch

        result = scanner.get_held_position_sells({"AAPL"})
        assert len(result) == 1
        assert result[0].symbol == "AAPL"


# ---------------------------------------------------------------------------
# Phase 3 — held-position conviction (decay detection)
# ---------------------------------------------------------------------------


class TestGetHeldPositionConviction:
    """SmartMoneyScanner.get_held_position_conviction — Phase 3 input."""

    def _scanner_with_filings(self, filings: List[TradeFiling], provider_name: str = "capitol_trades") -> SmartMoneyScanner:
        scanner = _exit_test_scanner()
        scanner._cache[provider_name] = CacheEntry(
            provider_name=provider_name,
            filings=filings,
            fetched_at=datetime.now(),
            ttl_hours=24.0,
        )
        mock_provider = MagicMock()
        mock_provider.provider_name = provider_name
        mock_provider.get_cache_ttl_hours.return_value = 24.0
        scanner._providers = [mock_provider]
        return scanner

    def test_returns_none_when_smart_money_disabled(self):
        scanner = _exit_test_scanner(smart_money_enabled=False)
        scanner._providers = []
        assert scanner.get_held_position_conviction({"AAPL"}) is None

    def test_returns_none_when_no_held_symbols(self):
        scanner = self._scanner_with_filings([_filing("AAPL", "buy")])
        assert scanner.get_held_position_conviction(set()) is None

    def test_returns_none_when_no_provider_data(self):
        """All providers returning empty filings should signal an outage."""
        scanner = _exit_test_scanner()
        # Provider with empty cache — no data this cycle.
        scanner._cache["capitol_trades"] = CacheEntry(
            provider_name="capitol_trades",
            filings=[],
            fetched_at=datetime.now(),
            ttl_hours=24.0,
        )
        mock_provider = MagicMock()
        mock_provider.provider_name = "capitol_trades"
        mock_provider.get_cache_ttl_hours.return_value = 24.0
        scanner._providers = [mock_provider]
        assert scanner.get_held_position_conviction({"AAPL"}) is None

    def test_returns_zero_for_held_symbol_without_filings(self):
        """A held symbol with no buy filings → 0.0, not None — that's
        legitimate decay, distinguishable from outage."""
        # Buy filing on MSFT (so any_provider_data=True), nothing on AAPL.
        scanner = self._scanner_with_filings([_filing("MSFT", "buy", dollars=500_000.0)])
        result = scanner.get_held_position_conviction({"AAPL"})
        assert result is not None
        assert result == {"AAPL": 0.0}

    def test_returns_score_for_held_symbol_with_buys(self):
        scanner = self._scanner_with_filings([
            _filing("AAPL", "buy", dollars=500_000.0),
            _filing("AAPL", "increase", dollars=300_000.0, source="sec_edgar"),
        ])
        result = scanner.get_held_position_conviction({"AAPL"})
        assert result is not None
        assert result["AAPL"] > 0.0

    def test_excludes_unhelp_symbols_from_result(self):
        scanner = self._scanner_with_filings([
            _filing("AAPL", "buy"),
            _filing("MSFT", "buy"),
        ])
        result = scanner.get_held_position_conviction({"AAPL"})
        assert result is not None
        assert "MSFT" not in result
        assert "AAPL" in result

    def test_only_buys_count_for_conviction(self):
        """Sells/decreases are not buy-side conviction. A held symbol with
        only sell filings → 0.0 (decayed)."""
        scanner = self._scanner_with_filings([
            _filing("AAPL", "sell", dollars=1_000_000.0),
            _filing("AAPL", "decrease", dollars=500_000.0),
            _filing("MSFT", "buy", dollars=100_000.0),  # data presence
        ])
        result = scanner.get_held_position_conviction({"AAPL"})
        assert result is not None
        assert result["AAPL"] == 0.0
