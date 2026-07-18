"""Property-based tests for PortfolioStore (Supabase-backed).

Uses the real Supabase connection. Each test creates a snapshot and cleans
it up after via CASCADE delete on portfolio_snapshots.
"""
from __future__ import annotations

from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from smart_trader.core.portfolio_scorer import FundHoldingRef, ScoredStock
from smart_trader.data.portfolio_store import PortfolioStore


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

ticker_st = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=5
)


def fund_holding_ref_st(fund_name_st=None):
    if fund_name_st is None:
        fund_name_st = st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"),
                                   blacklist_characters="\x00"),
            min_size=1, max_size=20,
        )
    return st.builds(
        FundHoldingRef,
        fund_name=fund_name_st,
        provider_name=st.sampled_from([
            "berkshire_13f", "ark_invest", "pershing_square_13f",
            "appaloosa_13f", "duquesne_13f",
        ]),
        holding_weight=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        share_count=st.integers(min_value=1, max_value=10_000_000),
        market_value=st.floats(min_value=1.0, max_value=1e12, allow_nan=False),
        as_of_date=st.just(datetime.now()),
    )


@st.composite
def scored_stock_st(draw, symbol_st=None):
    if symbol_st is None:
        symbol_st = ticker_st
    symbol = draw(symbol_st)
    n_funds = draw(st.integers(min_value=1, max_value=3))
    fund_names = draw(
        st.lists(st.text(min_size=1, max_size=20), min_size=n_funds, max_size=n_funds, unique=True)
    )
    funds = [draw(fund_holding_ref_st(fund_name_st=st.just(fn))) for fn in fund_names]
    return ScoredStock(
        symbol=symbol,
        composite_score=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        overlap_count=draw(st.integers(min_value=0, max_value=5)),
        average_holding_weight=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        performance_score=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        momentum_score=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        relative_strength=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        funds=funds,
    )


@st.composite
def unique_scored_stocks_st(draw, min_size=1, max_size=20):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    symbols = draw(
        st.lists(ticker_st, min_size=n, max_size=n, unique=True)
    )
    stocks = []
    for sym in symbols:
        stock = draw(scored_stock_st(symbol_st=st.just(sym)))
        stocks.append(stock)
    return stocks


def _make_store():
    return PortfolioStore(retention_days=90)


def _cleanup_snapshot(store, snap_id):
    """Delete a snapshot and its children from Supabase."""
    if snap_id and snap_id > 0:
        store._sb.delete("portfolio_stocks", {"snapshot_id": f"eq.{snap_id}"})
        store._sb.delete("portfolio_stock_funds", {"snapshot_id": f"eq.{snap_id}"})
        store._sb.delete("portfolio_snapshots", {"snapshot_id": f"eq.{snap_id}"})


# ---------------------------------------------------------------------------
# Property 10: Portfolio store round-trip preserves data
# ---------------------------------------------------------------------------

@given(
    scored=unique_scored_stocks_st(min_size=1, max_size=10),
    top_n_size=st.integers(min_value=1, max_value=30),
)
@settings(max_examples=10, deadline=None)
def test_round_trip_preserves_symbols_and_order(scored, top_n_size):
    """Save + load must preserve all symbols and their rank order."""
    store = _make_store()
    snap_id = None
    try:
        entry_prices = {}
        for i, s in enumerate(scored):
            if i < top_n_size and s.composite_score > 0:
                entry_prices[s.symbol] = 100.0 + i

        snap_id = store.save_snapshot(scored, top_n_size=top_n_size, entry_prices=entry_prices)
        loaded = store.load_latest()

        assert loaded is not None
        assert loaded.snapshot_id == snap_id
        assert loaded.universe_size == len(scored)

        input_symbols = [s.symbol for s in scored]
        loaded_symbols = [s.symbol for s in loaded.stocks]
        assert set(input_symbols) == set(loaded_symbols)

        for i, stock in enumerate(loaded.stocks):
            assert stock.rank == i + 1

        loaded_by_sym = {s.symbol: s for s in loaded.stocks}
        for s in scored:
            ls = loaded_by_sym[s.symbol]
            assert abs(ls.composite_score - s.composite_score) < 1e-6
    finally:
        _cleanup_snapshot(store, snap_id)


# ---------------------------------------------------------------------------
# Property 4: Top-N flag count invariant
# ---------------------------------------------------------------------------

@given(
    scored=unique_scored_stocks_st(min_size=1, max_size=10),
    top_n_size=st.integers(min_value=1, max_value=30),
)
@settings(max_examples=10, deadline=None)
def test_top_n_flag_count_invariant(scored, top_n_size):
    """count(in_top_n=True) == min(top_n_size, count_with_score > 0)."""
    scored.sort(key=lambda s: s.composite_score, reverse=True)

    store = _make_store()
    snap_id = None
    try:
        snap_id = store.save_snapshot(scored, top_n_size=top_n_size, entry_prices={})
        loaded = store.load_latest()

        assert loaded is not None

        positive_score_count = sum(1 for s in scored if s.composite_score > 0)
        expected_top_n = min(top_n_size, positive_score_count)
        actual_top_n = sum(1 for s in loaded.stocks if s.in_top_n)

        assert actual_top_n == expected_top_n

        for s in loaded.stocks:
            if s.composite_score == 0:
                assert not s.in_top_n
    finally:
        _cleanup_snapshot(store, snap_id)
