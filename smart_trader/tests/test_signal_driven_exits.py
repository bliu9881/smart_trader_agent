"""Phase 2 signal-driven exits — executor + main-loop integration.

Covers:
  * OrderExecutor.cancel_orders_for_symbol — only matching open trades
  * _evaluate_exit_signals — min-holding-period filter
  * _cycle bookkeeping — open-time tracking + cooldown GC
  * _cycle exit branch — dry-run vs live, cancel-then-close, ladder cleanup
  * Re-entry cooldown — blocks scanner candidates after a signal-driven exit

The OrderExecutor pieces mock ib_insync; the main-loop pieces construct
SmartTrader without connecting to IBKR (its __init__ doesn't reach the
network) and stub the components the methods touch.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from smart_trader.broker.order_executor import OrderExecutor
from smart_trader.core.signal import Signal
from smart_trader.core.smart_money import CandidateSymbol
from smart_trader.main import SmartTrader
from smart_trader.settings.config import AppConfig


# ---------------------------------------------------------------------------
# OrderExecutor.cancel_orders_for_symbol
# ---------------------------------------------------------------------------


def _mock_trade(symbol: str, order_id: int, done: bool = False) -> MagicMock:
    trade = MagicMock()
    trade.contract.symbol = symbol
    trade.order.orderId = order_id
    trade.isDone = MagicMock(return_value=done)
    return trade


def _executor_with_open_trades(open_trades: List[MagicMock]) -> OrderExecutor:
    client = MagicMock()
    client.ensure_connection.return_value = True
    client.ib.openTrades.return_value = open_trades
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.client = client
    executor.ib = client.ib
    executor._trade_log = []
    return executor


def test_cancel_orders_for_symbol_cancels_only_matching():
    aapl_parent = _mock_trade("AAPL", 1)
    aapl_stop = _mock_trade("AAPL", 2)
    msft_stop = _mock_trade("MSFT", 3)
    executor = _executor_with_open_trades([aapl_parent, aapl_stop, msft_stop])

    cancelled = executor.cancel_orders_for_symbol("AAPL")
    assert cancelled == 2
    cancel_calls = [call.args[0] for call in executor.ib.cancelOrder.call_args_list]
    assert aapl_parent.order in cancel_calls
    assert aapl_stop.order in cancel_calls
    assert msft_stop.order not in cancel_calls


def test_cancel_orders_for_symbol_skips_done_trades():
    done_trade = _mock_trade("AAPL", 1, done=True)
    live_trade = _mock_trade("AAPL", 2, done=False)
    executor = _executor_with_open_trades([done_trade, live_trade])

    cancelled = executor.cancel_orders_for_symbol("AAPL")
    assert cancelled == 1
    cancel_calls = [call.args[0] for call in executor.ib.cancelOrder.call_args_list]
    assert live_trade.order in cancel_calls
    assert done_trade.order not in cancel_calls


def test_cancel_orders_for_symbol_returns_zero_when_no_match():
    executor = _executor_with_open_trades([_mock_trade("MSFT", 1)])
    assert executor.cancel_orders_for_symbol("AAPL") == 0
    executor.ib.cancelOrder.assert_not_called()


def test_cancel_orders_for_symbol_returns_zero_when_disconnected():
    client = MagicMock()
    client.ensure_connection.return_value = False
    executor = OrderExecutor.__new__(OrderExecutor)
    executor.client = client
    executor.ib = client.ib
    executor._trade_log = []
    assert executor.cancel_orders_for_symbol("AAPL") == 0
    client.ib.openTrades.assert_not_called()


def test_cancel_orders_for_symbol_continues_after_individual_failure():
    """A single cancel raising should not abort the rest of the loop."""
    failing = _mock_trade("AAPL", 1)
    succeeding = _mock_trade("AAPL", 2)
    executor = _executor_with_open_trades([failing, succeeding])
    executor.ib.cancelOrder.side_effect = [RuntimeError("boom"), None]
    # Returns count of cancels we attempted to issue minus the failure
    cancelled = executor.cancel_orders_for_symbol("AAPL")
    assert cancelled == 1
    assert executor.ib.cancelOrder.call_count == 2


# ---------------------------------------------------------------------------
# SmartTrader._evaluate_exit_signals — min-holding-period guard
# ---------------------------------------------------------------------------


def _sm_candidate(symbol: str, score: float = 5.0) -> CandidateSymbol:
    now = datetime.now()
    return CandidateSymbol(
        symbol=symbol,
        conviction_score=score,
        sources=["capitol_trades"],
        actors=["actor"],
        total_dollar_volume=200_000.0,
        filing_count=1,
        most_recent_filing=now - timedelta(days=2),
    )


def _trader(config: Optional[AppConfig] = None) -> SmartTrader:
    """Build a SmartTrader without connecting. Components used by methods
    under test must be stubbed by the caller."""
    return SmartTrader(config or AppConfig(), dry_run=True)


def _stub_market_data(t: SmartTrader, price: float = 100.0) -> None:
    t.market_data = MagicMock()
    bars = MagicMock()
    bars.__len__ = MagicMock(return_value=1)
    bars.__getitem__ = MagicMock(return_value=MagicMock(iloc=MagicMock(__getitem__=lambda self, i: price)))
    # Easier: have _get_latest_price return a price by patching directly later.
    t.market_data.get_historical_bars.return_value = None
    # Override _get_latest_price entirely
    t._get_latest_price = lambda symbol: price  # type: ignore[assignment]


def test_evaluate_exit_signals_blocks_too_young_position():
    t = _trader()
    t.config.exits.min_holding_period_days = 14
    t.config.exits.min_exit_conviction_score = 0.0  # focus on holding gate
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    # Position opened 5 days ago — under the 14-day floor.
    t._position_open_ts["AAPL"] = datetime.now() - timedelta(days=5)
    positions = {"AAPL": {"market_value": 1_000.0}}

    out = t._evaluate_exit_signals(positions)
    assert out == []


def test_evaluate_exit_signals_allows_old_enough_position():
    t = _trader()
    t.config.exits.min_holding_period_days = 14
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    t._position_open_ts["AAPL"] = datetime.now() - timedelta(days=30)
    positions = {"AAPL": {"market_value": 1_000.0}}

    out = t._evaluate_exit_signals(positions)
    assert len(out) == 1
    assert out[0].symbol == "AAPL"
    assert out[0].direction == "FLAT"


def test_evaluate_exit_signals_holding_period_zero_disables_guard():
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    # Even with no recorded open time, guard is disabled.
    positions = {"AAPL": {"market_value": 1_000.0}}

    out = t._evaluate_exit_signals(positions)
    assert len(out) == 1


def test_evaluate_exit_signals_conviction_floor_filters_low_signals():
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 5.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [
        _sm_candidate("WEAK", score=2.0),
        _sm_candidate("STRONG", score=8.0),
    ]
    _stub_market_data(t)
    positions = {"WEAK": {}, "STRONG": {}}

    out = t._evaluate_exit_signals(positions)
    symbols = [s.symbol for s in out]
    assert "STRONG" in symbols
    assert "WEAK" not in symbols


def test_evaluate_exit_signals_short_circuits_when_disabled():
    t = _trader()
    t.config.exits.enabled = False
    t.scanner = MagicMock()
    out = t._evaluate_exit_signals({"AAPL": {}})
    assert out == []
    t.scanner.get_held_position_sells.assert_not_called()


def test_evaluate_exit_signals_short_circuits_when_no_scanner():
    t = _trader()
    t.scanner = None
    out = t._evaluate_exit_signals({"AAPL": {}})
    assert out == []


def test_evaluate_exit_signals_handles_scanner_exception():
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.side_effect = RuntimeError("upstream broken")
    _stub_market_data(t)
    out = t._evaluate_exit_signals({"AAPL": {}})
    assert out == []


# ---------------------------------------------------------------------------
# SmartTrader._update_top_n_history — Phase 2b bookkeeping
# ---------------------------------------------------------------------------


def test_update_top_n_history_records_held_symbols_in_top_n():
    t = _trader()
    t._update_top_n_history({"AAPL", "MSFT", "GOOG"}, {"AAPL", "MSFT"})
    assert "AAPL" in t._held_top_n_history
    assert "MSFT" in t._held_top_n_history
    # GOOG isn't held → no entry.
    assert "GOOG" not in t._held_top_n_history


def test_update_top_n_history_skips_when_top_n_empty():
    """Empty top-N (pipeline failure / pre-first-refresh) must NOT update —
    otherwise the next non-empty refresh would see fabricated drop-outs."""
    t = _trader()
    # Seed prior history.
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=5)
    seed_ts = t._held_top_n_history["AAPL"]
    t._update_top_n_history(set(), {"AAPL"})
    # History unchanged — same datetime object.
    assert t._held_top_n_history["AAPL"] == seed_ts


def test_update_top_n_history_drops_unheld_symbols():
    t = _trader()
    t._held_top_n_history["AAPL"] = datetime.now()
    t._held_top_n_history["MSFT"] = datetime.now()
    # MSFT no longer held this cycle.
    t._update_top_n_history({"AAPL"}, {"AAPL"})
    assert "AAPL" in t._held_top_n_history
    assert "MSFT" not in t._held_top_n_history


def test_update_top_n_history_does_not_add_unheld_top_n_symbols():
    t = _trader()
    t._update_top_n_history({"AAPL", "MSFT"}, set())
    assert t._held_top_n_history == {}


# ---------------------------------------------------------------------------
# SmartTrader._top_n_dropout_exits — Phase 2b trigger
# ---------------------------------------------------------------------------


def test_dropout_fires_after_threshold_days_out():
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    # AAPL last seen in top-N 5 days ago, currently not in top-N.
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=5)
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT", "GOOG"})
    assert len(out) == 1
    assert out[0].symbol == "AAPL"
    assert out[0].metadata["exit_trigger"] == "top_n_dropout"
    assert out[0].metadata["days_out_of_top_n"] >= 3


def test_dropout_does_not_fire_within_threshold():
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=1)
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert out == []


def test_dropout_does_not_fire_for_unobserved_symbol():
    """Held symbol never seen in top-N (manual hold or pre-bot position)
    must not trigger drop-out."""
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    # _held_top_n_history is empty for AAPL.
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert out == []


def test_dropout_does_not_fire_when_top_n_empty():
    """A transient empty top-N must not cascade-fire drop-outs."""
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=5)
    out = t._top_n_dropout_exits({"AAPL": {}}, set())
    assert out == []


def test_dropout_skipped_when_symbol_back_in_top_n():
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    # AAPL is back in top-N this cycle.
    out = t._top_n_dropout_exits({"AAPL": {}}, {"AAPL", "MSFT"})
    assert out == []


def test_dropout_respects_min_holding_period():
    t = _trader()
    t.config.exits.top_n_dropout_days_required = 3
    t.config.exits.min_holding_period_days = 14
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=5)
    t._position_open_ts["AAPL"] = datetime.now() - timedelta(days=3)  # too young
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert out == []


def test_dropout_disabled_via_config():
    t = _trader()
    t.config.exits.on_top_n_dropout = False
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert out == []


def test_dropout_metadata_carries_per_trigger_dry_run():
    """When global dry_run is False but top_n_dropout_dry_run is True,
    drop-out signals must still encode dry_run=True in metadata."""
    t = _trader()
    t.config.exits.dry_run = False  # smart-money sells go live
    t.config.exits.top_n_dropout_dry_run = True  # drop-outs preview only
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert len(out) == 1
    assert out[0].metadata["dry_run"] is True


def test_dropout_metadata_live_when_both_dry_run_off():
    t = _trader()
    t.config.exits.dry_run = False
    t.config.exits.top_n_dropout_dry_run = False
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._top_n_dropout_exits({"AAPL": {}}, {"MSFT"})
    assert len(out) == 1
    assert out[0].metadata["dry_run"] is False


# ---------------------------------------------------------------------------
# SmartTrader._evaluate_exit_signals — combination + dedupe
# ---------------------------------------------------------------------------


def test_evaluate_combines_smart_money_and_dropout_exits():
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    t._held_top_n_history["MSFT"] = datetime.now() - timedelta(days=10)
    out = t._evaluate_exit_signals({"AAPL": {}, "MSFT": {}}, {"GOOG"})
    triggers = {s.symbol: s.metadata["exit_trigger"] for s in out}
    assert triggers == {"AAPL": "smart_money_sell", "MSFT": "top_n_dropout"}


def test_evaluate_smart_money_takes_priority_over_dropout():
    """If a single symbol fires from BOTH triggers, smart-money-sell wins."""
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._evaluate_exit_signals({"AAPL": {}}, {"MSFT"})
    assert len(out) == 1
    assert out[0].metadata["exit_trigger"] == "smart_money_sell"


# ---------------------------------------------------------------------------
# SmartTrader._update_conviction_history — Phase 3 bookkeeping
# ---------------------------------------------------------------------------


def test_update_conviction_history_sets_below_since_for_eligible_symbol():
    t = _trader()
    t.config.exits.min_held_conviction_score = 4.0
    # Eligible: previously observed in top-N.
    t._held_top_n_history["AAPL"] = datetime.now()
    t._update_conviction_history({"AAPL": 1.5}, {"AAPL"})
    assert "AAPL" in t._held_conviction_below_since


def test_update_conviction_history_skips_ineligible_symbol():
    """Symbol never observed in top-N (manual hold) doesn't get tracked."""
    t = _trader()
    t.config.exits.min_held_conviction_score = 4.0
    # No entry in _held_top_n_history.
    t._update_conviction_history({"AAPL": 1.5}, {"AAPL"})
    assert "AAPL" not in t._held_conviction_below_since


def test_update_conviction_history_clears_when_score_recovers():
    t = _trader()
    t.config.exits.min_held_conviction_score = 4.0
    t._held_top_n_history["AAPL"] = datetime.now()
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=2)
    # Score now above threshold → clear.
    t._update_conviction_history({"AAPL": 6.0}, {"AAPL"})
    assert "AAPL" not in t._held_conviction_below_since


def test_update_conviction_history_preserves_first_dip_time():
    """Once below_since is set, subsequent below-threshold observations
    must NOT overwrite it (we want the first dip, not the latest)."""
    t = _trader()
    t.config.exits.min_held_conviction_score = 4.0
    t._held_top_n_history["AAPL"] = datetime.now()
    first_dip = datetime.now() - timedelta(days=5)
    t._held_conviction_below_since["AAPL"] = first_dip
    t._update_conviction_history({"AAPL": 1.0}, {"AAPL"})
    assert t._held_conviction_below_since["AAPL"] == first_dip


def test_update_conviction_history_skips_when_scores_none():
    """Outage (scores=None) must not alter state — would otherwise
    fabricate a portfolio-wide decay event."""
    t = _trader()
    t._held_top_n_history["AAPL"] = datetime.now()
    seed = datetime.now() - timedelta(days=2)
    t._held_conviction_below_since["AAPL"] = seed
    t._update_conviction_history(None, {"AAPL"})
    assert t._held_conviction_below_since["AAPL"] == seed


def test_update_conviction_history_drops_unheld_symbols():
    t = _trader()
    t._held_conviction_below_since["AAPL"] = datetime.now()
    t._update_conviction_history({"MSFT": 5.0}, {"MSFT"})
    assert "AAPL" not in t._held_conviction_below_since


# ---------------------------------------------------------------------------
# SmartTrader._conviction_decay_exits — Phase 3 trigger
# ---------------------------------------------------------------------------


def test_decay_fires_after_threshold_days_below():
    t = _trader()
    t.config.exits.conviction_decay_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=5)
    out = t._conviction_decay_exits({"AAPL": {}})
    assert len(out) == 1
    assert out[0].symbol == "AAPL"
    assert out[0].metadata["exit_trigger"] == "conviction_decay"
    assert out[0].metadata["days_below_threshold"] >= 3


def test_decay_does_not_fire_within_threshold():
    t = _trader()
    t.config.exits.conviction_decay_days_required = 3
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=1)
    assert t._conviction_decay_exits({"AAPL": {}}) == []


def test_decay_does_not_fire_for_symbol_with_no_below_since():
    t = _trader()
    _stub_market_data(t)
    # _held_conviction_below_since empty for AAPL.
    assert t._conviction_decay_exits({"AAPL": {}}) == []


def test_decay_respects_min_holding_period():
    t = _trader()
    t.config.exits.conviction_decay_days_required = 3
    t.config.exits.min_holding_period_days = 14
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    t._position_open_ts["AAPL"] = datetime.now() - timedelta(days=5)  # too young
    assert t._conviction_decay_exits({"AAPL": {}}) == []


def test_decay_disabled_via_config():
    t = _trader()
    t.config.exits.on_conviction_decay = False
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    assert t._conviction_decay_exits({"AAPL": {}}) == []


def test_decay_metadata_carries_per_trigger_dry_run():
    t = _trader()
    t.config.exits.dry_run = False
    t.config.exits.conviction_decay_dry_run = True  # preview only
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._conviction_decay_exits({"AAPL": {}})
    assert len(out) == 1
    assert out[0].metadata["dry_run"] is True


def test_decay_metadata_live_when_both_dry_run_off():
    t = _trader()
    t.config.exits.dry_run = False
    t.config.exits.conviction_decay_dry_run = False
    t.config.exits.min_holding_period_days = 0
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._conviction_decay_exits({"AAPL": {}})
    assert out[0].metadata["dry_run"] is False


# ---------------------------------------------------------------------------
# Three-trigger priority dedupe in _evaluate_exit_signals
# ---------------------------------------------------------------------------


def test_evaluate_priority_smart_money_over_decay():
    """Smart-money-sell wins over conviction-decay when both fire."""
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._evaluate_exit_signals({"AAPL": {}}, set())
    assert len(out) == 1
    assert out[0].metadata["exit_trigger"] == "smart_money_sell"


def test_evaluate_priority_dropout_over_decay():
    """Drop-out wins over decay when both fire on the same symbol."""
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = []
    _stub_market_data(t)
    t._held_top_n_history["AAPL"] = datetime.now() - timedelta(days=10)
    t._held_conviction_below_since["AAPL"] = datetime.now() - timedelta(days=10)
    out = t._evaluate_exit_signals({"AAPL": {}}, {"MSFT"})
    assert len(out) == 1
    assert out[0].metadata["exit_trigger"] == "top_n_dropout"


def test_evaluate_combines_three_triggers_distinct_symbols():
    """Each symbol fires from a different trigger — all three returned."""
    t = _trader()
    t.config.exits.min_holding_period_days = 0
    t.config.exits.min_exit_conviction_score = 0.0
    t.scanner = MagicMock()
    t.scanner.get_held_position_sells.return_value = [_sm_candidate("AAPL")]
    _stub_market_data(t)
    # MSFT: dropped out of top-N
    t._held_top_n_history["MSFT"] = datetime.now() - timedelta(days=10)
    # GOOG: conviction decay only (never observed in top-N here, so just
    # below_since is set directly to simulate eligibility)
    t._held_conviction_below_since["GOOG"] = datetime.now() - timedelta(days=10)
    out = t._evaluate_exit_signals(
        {"AAPL": {}, "MSFT": {}, "GOOG": {}}, {"NVDA"}
    )
    by_sym = {s.symbol: s.metadata["exit_trigger"] for s in out}
    assert by_sym == {
        "AAPL": "smart_money_sell",
        "MSFT": "top_n_dropout",
        "GOOG": "conviction_decay",
    }


# ---------------------------------------------------------------------------
# Restart-persistence — _restore_exit_state and _persist_exit_state
# ---------------------------------------------------------------------------


def test_restore_exit_state_hydrates_all_four_dicts():
    t = _trader()
    ts1 = datetime(2026, 4, 1)
    ts2 = datetime(2026, 4, 10)
    t.portfolio_store = MagicMock()
    t.portfolio_store.load_exit_state.return_value = {
        "position_open_ts": {"AAPL": ts1},
        "recent_exits": {"GOOG": ts2},
        "top_n_history": {"AAPL": ts1, "MSFT": ts2},
        "conviction_below_since": {"NVDA": ts1},
    }
    t._restore_exit_state()
    assert t._position_open_ts == {"AAPL": ts1}
    assert t._recent_exits == {"GOOG": ts2}
    assert t._held_top_n_history == {"AAPL": ts1, "MSFT": ts2}
    assert t._held_conviction_below_since == {"NVDA": ts1}


def test_restore_exit_state_no_op_when_store_missing():
    t = _trader()
    t.portfolio_store = None
    # Pre-existing state must not be clobbered.
    seed = datetime.now()
    t._position_open_ts["AAPL"] = seed
    t._restore_exit_state()
    assert t._position_open_ts == {"AAPL": seed}


def test_restore_exit_state_recovers_from_load_failure():
    """Store raising during load → log + continue with prior state."""
    t = _trader()
    t.portfolio_store = MagicMock()
    t.portfolio_store.load_exit_state.side_effect = RuntimeError("DB locked")
    t._position_open_ts["AAPL"] = datetime.now()
    t._restore_exit_state()
    # Pre-existing state intact (load failure doesn't clobber).
    assert "AAPL" in t._position_open_ts


def test_persist_exit_state_writes_all_four_dicts():
    t = _trader()
    t.portfolio_store = MagicMock()
    ts1 = datetime(2026, 4, 1)
    t._position_open_ts = {"AAPL": ts1}
    t._recent_exits = {"GOOG": ts1}
    t._held_top_n_history = {"MSFT": ts1}
    t._held_conviction_below_since = {"NVDA": ts1}
    t._persist_exit_state()
    t.portfolio_store.save_exit_state.assert_called_once()
    payload = t.portfolio_store.save_exit_state.call_args.args[0]
    assert payload == {
        "position_open_ts": {"AAPL": ts1},
        "recent_exits": {"GOOG": ts1},
        "top_n_history": {"MSFT": ts1},
        "conviction_below_since": {"NVDA": ts1},
    }


def test_persist_exit_state_no_op_when_store_missing():
    t = _trader()
    t.portfolio_store = None
    # Just shouldn't raise.
    t._persist_exit_state()


def test_persist_exit_state_swallows_save_failure():
    """A failed persist must not abort the trading loop."""
    t = _trader()
    t.portfolio_store = MagicMock()
    t.portfolio_store.save_exit_state.side_effect = RuntimeError("disk full")
    # Just shouldn't raise.
    t._persist_exit_state()
