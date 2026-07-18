"""Tests for PortfolioStore — Supabase-backed persistence.

Uses the real Supabase connection from .env. Each test cleans up after itself
by deleting the snapshot it created (CASCADE deletes stocks + funds).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from smart_trader.core.portfolio_scorer import FundHoldingRef, ScoredStock
from smart_trader.data.portfolio_store import PortfolioStore


def _scored(symbol: str, score: float, funds: int = 1) -> ScoredStock:
    return ScoredStock(
        symbol=symbol,
        composite_score=score,
        overlap_count=funds,
        average_holding_weight=0.10,
        performance_score=0.5,
        momentum_score=0.5,
        relative_strength=0.5,
        funds=[
            FundHoldingRef(
                fund_name=f"TestFund{i}",
                provider_name=f"test_provider_{i}",
                holding_weight=0.10,
                share_count=1000,
                market_value=100_000.0,
                as_of_date=datetime.now(),
            )
            for i in range(funds)
        ],
    )


@pytest.fixture
def store():
    return PortfolioStore(retention_days=90)


@pytest.fixture
def snapshot_cleanup(store):
    """Track snapshot_ids created during a test and delete them after."""
    created = []
    yield created
    for sid in created:
        store._sb.delete("portfolio_stocks", {"snapshot_id": f"eq.{sid}"})
        store._sb.delete("portfolio_stock_funds", {"snapshot_id": f"eq.{sid}"})
        store._sb.delete("portfolio_snapshots", {"snapshot_id": f"eq.{sid}"})


# ---------------------------------------------------------------------------
# save_snapshot + load_latest
# ---------------------------------------------------------------------------

def test_save_and_load_round_trip(store, snapshot_cleanup):
    scored = [_scored("TSTAAA", 0.9), _scored("TSTBBB", 0.5), _scored("TSTCCC", 0.0)]
    entry_prices = {"TSTAAA": 150.0, "TSTBBB": 80.0}
    snap_id = store.save_snapshot(scored, top_n_size=2, entry_prices=entry_prices)
    snapshot_cleanup.append(snap_id)

    assert snap_id > 0

    loaded = store.load_latest()
    assert loaded is not None
    assert loaded.snapshot_id == snap_id
    assert loaded.universe_size == 3
    assert loaded.top_n_size == 2
    symbols = [s.symbol for s in loaded.stocks]
    assert "TSTAAA" in symbols
    assert "TSTBBB" in symbols
    assert "TSTCCC" in symbols


def test_in_top_n_flag_respects_rank_and_positive_score(store, snapshot_cleanup):
    scored = [_scored("TSTAAA", 0.9), _scored("TSTBBB", 0.5), _scored("TSTCCC", 0.0)]
    snap_id = store.save_snapshot(scored, top_n_size=5, entry_prices={})
    snapshot_cleanup.append(snap_id)

    loaded = store.load_latest()
    by_sym = {s.symbol: s for s in loaded.stocks}
    assert by_sym["TSTAAA"].in_top_n is True
    assert by_sym["TSTBBB"].in_top_n is True
    assert by_sym["TSTCCC"].in_top_n is False  # zero score


def test_optimal_entry_price_null_for_non_top_n(store, snapshot_cleanup):
    scored = [_scored("TSTAAA", 0.9), _scored("TSTBBB", 0.5)]
    snap_id = store.save_snapshot(scored, top_n_size=1, entry_prices={"TSTAAA": 150.0, "TSTBBB": 80.0})
    snapshot_cleanup.append(snap_id)

    loaded = store.load_latest()
    by_sym = {s.symbol: s for s in loaded.stocks}
    assert by_sym["TSTAAA"].optimal_entry_price == 150.0
    assert by_sym["TSTBBB"].optimal_entry_price is None


def test_funds_round_trip(store, snapshot_cleanup):
    scored = [_scored("TSTAAA", 0.9, funds=3)]
    snap_id = store.save_snapshot(scored, top_n_size=1, entry_prices={})
    snapshot_cleanup.append(snap_id)

    loaded = store.load_latest()
    aaa = [s for s in loaded.stocks if s.symbol == "TSTAAA"][0]
    assert len(aaa.funds) == 3
    assert {f["provider_name"] for f in aaa.funds} == {
        "test_provider_0", "test_provider_1", "test_provider_2"
    }


def test_load_latest_returns_data(store):
    """Verify load_latest returns something (prior tests or populate script left data)."""
    # This just verifies the read path works against real Supabase.
    # It may return None if the DB is empty, which is also valid.
    result = store.load_latest()
    assert result is None or result.snapshot_id > 0


# ---------------------------------------------------------------------------
# store_raw_holdings
# ---------------------------------------------------------------------------

def _fund_holdings(fund="TestTCI", provider="test_tci_13f", symbol="TSTAAPL", d=None):
    from smart_trader.core.smart_money import FundHoldings
    return FundHoldings(
        fund_name=fund,
        provider_name=provider,
        symbol=symbol,
        share_count=1000,
        holding_weight=0.05,
        market_value=200_000.0,
        as_of_date=d or datetime(2099, 1, 1),  # far future to avoid collision
    )


@pytest.fixture
def holdings_cleanup(store):
    """Clean up test holdings after the test."""
    yield
    store._sb.delete("fund_holdings_raw", {"fund_name": "eq.TestTCI"})
    store._sb.delete("fund_holdings_raw", {"fund_name": "eq.TestBaupost"})


def test_store_raw_holdings_round_trip(store, holdings_cleanup):
    holdings = [
        _fund_holdings(fund="TestTCI", symbol="TSTAAPL"),
        _fund_holdings(fund="TestTCI", symbol="TSTMSFT"),
        _fund_holdings(fund="TestBaupost", provider="test_baupost_13f", symbol="TSTAAPL"),
    ]
    inserted = store.store_raw_holdings(holdings)
    assert inserted == 3


def test_store_raw_holdings_empty_list_is_noop(store):
    assert store.store_raw_holdings([]) == 0


# ---------------------------------------------------------------------------
# exit state
# ---------------------------------------------------------------------------

@pytest.fixture
def exit_state_cleanup(store):
    """Clean up exit_state_kv test entries after the test."""
    yield
    for key in ("position_open_ts", "recent_exits", "top_n_history", "conviction_below_since"):
        store._sb.delete("exit_state_kv", {"key": f"eq.{key}"})


def test_load_exit_state_returns_canonical_keys(store, exit_state_cleanup):
    # Clear any existing state first
    for key in ("position_open_ts", "recent_exits", "top_n_history", "conviction_below_since"):
        store._sb.delete("exit_state_kv", {"key": f"eq.{key}"})

    state = store.load_exit_state()
    assert set(state.keys()) == {
        "position_open_ts", "recent_exits",
        "top_n_history", "conviction_below_since",
    }


def test_save_and_load_exit_state_round_trip(store, exit_state_cleanup):
    ts1 = datetime(2026, 4, 1, 10, 30, 0)
    ts2 = datetime(2026, 4, 15, 14, 0, 0)
    state = {
        "position_open_ts": {"AAPL": ts1, "MSFT": ts2},
        "recent_exits": {"GOOG": ts1},
        "top_n_history": {"AAPL": ts2},
        "conviction_below_since": {"NVDA": ts1},
    }
    store.save_exit_state(state)
    loaded = store.load_exit_state()
    assert loaded["position_open_ts"] == {"AAPL": ts1, "MSFT": ts2}
    assert loaded["recent_exits"] == {"GOOG": ts1}
    assert loaded["top_n_history"] == {"AAPL": ts2}
    assert loaded["conviction_below_since"] == {"NVDA": ts1}


def test_save_exit_state_replaces_prior_value(store, exit_state_cleanup):
    store.save_exit_state({
        "position_open_ts": {"AAPL": datetime(2026, 1, 1)},
        "recent_exits": {},
        "top_n_history": {},
        "conviction_below_since": {},
    })
    store.save_exit_state({
        "position_open_ts": {},
        "recent_exits": {},
        "top_n_history": {},
        "conviction_below_since": {},
    })
    loaded = store.load_exit_state()
    assert loaded["position_open_ts"] == {}


def test_save_exit_state_drops_unknown_keys(store, exit_state_cleanup):
    store.save_exit_state({
        "position_open_ts": {"AAPL": datetime(2026, 1, 1)},
        "bogus_key": {"X": datetime(2026, 1, 1)},
        "recent_exits": {},
        "top_n_history": {},
        "conviction_below_since": {},
    })
    loaded = store.load_exit_state()
    assert "bogus_key" not in loaded
    assert loaded["position_open_ts"] == {"AAPL": datetime(2026, 1, 1)}
