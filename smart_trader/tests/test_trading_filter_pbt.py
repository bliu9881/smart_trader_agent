"""Property-based tests for TradingFilter.

Tests correctness properties from the design document:
  - Property 7: Path B filter output is subset of Top_N_Set
  - Property 8: Path A signals only for price ≤ entry
  - Property 9: Overlap signals require both Path A and Path B conditions
"""
from __future__ import annotations

from datetime import datetime

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from smart_trader.core.smart_money import CandidateSymbol
from smart_trader.core.trading_filter import TradingFilter, TopNStock
from smart_trader.settings.config import SmartMoneyConfig, TraderConfig


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

ticker_st = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=5
)


def candidate_st(symbol_st=None):
    if symbol_st is None:
        symbol_st = ticker_st
    return st.builds(
        CandidateSymbol,
        symbol=symbol_st,
        conviction_score=st.floats(min_value=0.0, max_value=20.0, allow_nan=False),
        sources=st.just(["capitol_trades"]),
        actors=st.just(["TestActor"]),
        total_dollar_volume=st.floats(min_value=0.0, max_value=1e9, allow_nan=False),
        filing_count=st.integers(min_value=1, max_value=100),
        most_recent_filing=st.just(datetime.now()),
    )


def top_n_stock_st(symbol_st=None):
    if symbol_st is None:
        symbol_st = ticker_st
    return st.builds(
        TopNStock,
        symbol=symbol_st,
        composite_score=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        optimal_entry_price=st.floats(min_value=1.0, max_value=10000.0, allow_nan=False),
        overlap_count=st.integers(min_value=1, max_value=5),
        sources=st.just(["berkshire_13f"]),
    )


# ---------------------------------------------------------------------------
# Property 7: Path B filter output is subset of Top_N_Set
# ---------------------------------------------------------------------------

@given(
    candidates=st.lists(candidate_st(), min_size=0, max_size=20),
    top_n_symbols=st.frozensets(ticker_st, min_size=0, max_size=15),
    portfolio_symbols=st.frozensets(ticker_st, min_size=0, max_size=30),
)
@settings(max_examples=200, deadline=None)
def test_path_b_kept_symbols_subset_of_top_n(candidates, top_n_symbols, portfolio_symbols):
    """Every kept candidate must have its symbol in top_n_symbols."""
    # Ensure top_n is a subset of portfolio (as it would be in production)
    full_portfolio = portfolio_symbols | top_n_symbols

    tf = TradingFilter(SmartMoneyConfig(), TraderConfig())
    outcomes = tf.filter_scanner_candidates(
        candidates=candidates,
        portfolio_symbols=full_portfolio,
        top_n_symbols=set(top_n_symbols),
    )

    for outcome in outcomes:
        if outcome.kept:
            assert outcome.symbol in top_n_symbols, (
                f"Kept symbol {outcome.symbol} not in top_n_symbols"
            )


# ---------------------------------------------------------------------------
# Property 8: Path A signals only for price ≤ entry
# ---------------------------------------------------------------------------

@given(
    data=st.data(),
    n_stocks=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=200, deadline=None)
def test_path_a_signals_only_when_price_leq_entry(data, n_stocks):
    """generate_entry_signals() must only produce signals where price ≤ entry."""
    tf = TradingFilter(SmartMoneyConfig(), TraderConfig())

    stocks = []
    current_prices = {}
    for _ in range(n_stocks):
        stock = data.draw(top_n_stock_st())
        # Avoid duplicate symbols
        if stock.symbol in {s.symbol for s in stocks}:
            continue
        stocks.append(stock)
        # Generate a price that may or may not be below entry
        price = data.draw(
            st.floats(min_value=0.01, max_value=20000.0, allow_nan=False, allow_infinity=False)
        )
        current_prices[stock.symbol] = price

    signals = tf.generate_entry_signals(
        top_n=stocks,
        held_symbols=set(),
        current_prices=current_prices,
    )

    tolerance = tf.sm_config.entry_price_tolerance
    for sig in signals:
        stock = next(s for s in stocks if s.symbol == sig.symbol)
        price = current_prices[sig.symbol]
        max_price = stock.optimal_entry_price * (1.0 + tolerance)
        assert price <= max_price, (
            f"Signal for {sig.symbol}: price {price} > entry*{1+tolerance} = {max_price}"
        )

    # Also verify: no signal for stocks where price is outside the tolerance band
    signaled_symbols = {s.symbol for s in signals}
    for stock in stocks:
        if stock.symbol in current_prices:
            price = current_prices[stock.symbol]
            max_price = stock.optimal_entry_price * (1.0 + tolerance)
            if price > max_price:
                assert stock.symbol not in signaled_symbols, (
                    f"Signal generated for {stock.symbol} despite price {price} > tolerance-band max {max_price}"
                )


# ---------------------------------------------------------------------------
# Property 9: Overlap signals require both Path A and Path B conditions
# ---------------------------------------------------------------------------

@given(
    data=st.data(),
    n_stocks=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=200, deadline=None)
def test_overlap_requires_both_path_a_and_path_b(data, n_stocks):
    """Every overlap signal must satisfy: price ≤ entry AND scanner conviction ≥ threshold."""
    tf = TradingFilter(SmartMoneyConfig(), TraderConfig())
    min_conviction = 5.0

    stocks = []
    candidates = []
    current_prices = {}
    seen_symbols = set()

    for _ in range(n_stocks):
        stock = data.draw(top_n_stock_st())
        if stock.symbol in seen_symbols:
            continue
        seen_symbols.add(stock.symbol)
        stocks.append(stock)

        # Maybe generate a scanner candidate for this symbol
        has_scanner = data.draw(st.booleans())
        if has_scanner:
            cand = data.draw(candidate_st(symbol_st=st.just(stock.symbol)))
            candidates.append(cand)

        # Generate a price
        price = data.draw(
            st.floats(min_value=0.01, max_value=20000.0, allow_nan=False, allow_infinity=False)
        )
        current_prices[stock.symbol] = price

    signals = tf.generate_overlap_signals(
        top_n=stocks,
        scanner_candidates=candidates,
        held_symbols=set(),
        current_prices=current_prices,
        min_conviction_score=min_conviction,
    )

    scanner_by_sym = {c.symbol: c for c in candidates}

    tolerance = tf.sm_config.entry_price_tolerance
    for sig in signals:
        sym = sig.symbol
        stock = next(s for s in stocks if s.symbol == sym)
        price = current_prices[sym]

        # Path A condition: price within entry-tolerance band
        max_price = stock.optimal_entry_price * (1.0 + tolerance)
        assert price <= max_price, (
            f"Overlap {sym}: price {price} > tolerance-band max {max_price}"
        )

        # Path B condition: scanner candidate with sufficient conviction
        assert sym in scanner_by_sym, (
            f"Overlap {sym}: no scanner candidate found"
        )
        assert scanner_by_sym[sym].conviction_score >= min_conviction, (
            f"Overlap {sym}: conviction {scanner_by_sym[sym].conviction_score} < {min_conviction}"
        )

        # Metadata checks
        assert sig.metadata.get("overlap_boost") is True
        assert sig.metadata.get("risk_multiplier") == 2.0
