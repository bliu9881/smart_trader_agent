"""Tests for TradingFilter (Path A entry triggers, Path B scanner filter)."""
from __future__ import annotations

from datetime import datetime

import pytest

from smart_trader.core.smart_money import CandidateSymbol
from smart_trader.core.trading_filter import TopNStock, TradingFilter, is_tradable
from smart_trader.settings.config import SmartMoneyConfig, TraderConfig


@pytest.fixture
def tf() -> TradingFilter:
    return TradingFilter(SmartMoneyConfig(), TraderConfig())


def _top_n(symbol: str, entry: float = 100.0) -> TopNStock:
    return TopNStock(
        symbol=symbol,
        composite_score=0.7,
        optimal_entry_price=entry,
        overlap_count=3,
        sources=["berkshire_13f"],
    )


def _candidate(symbol: str) -> CandidateSymbol:
    return CandidateSymbol(
        symbol=symbol,
        conviction_score=7.0,
        sources=["capitol_trades"],
        actors=["X"],
        total_dollar_volume=100_000.0,
        filing_count=1,
        most_recent_filing=datetime.now(),
    )


# ---------------------------------------------------------------- Path A

def test_entry_signal_fires_when_price_at_or_below_entry(tf):
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 99.0},
    )
    assert len(signals) == 1
    assert signals[0].symbol == "AAPL"
    assert signals[0].strategy_name == "SmartMoneyPortfolio_Entry"
    assert signals[0].metadata["smart_money_portfolio"] is True
    assert signals[0].entry_price == 99.0


def test_entry_signal_skipped_when_price_above_tolerance_band(tf):
    # Default tolerance is 5%; 110 is above 100 * 1.05 = 105 → no signal
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 110.0},
    )
    assert signals == []


def test_entry_signal_fires_within_tolerance_band(tf):
    # Default tolerance is 5%; 104 is within 100 * 1.05 → trigger
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 104.0},
    )
    assert len(signals) == 1
    assert signals[0].entry_price == 104.0  # records actual market price, not the ideal entry


def test_entry_signal_tolerance_zero_requires_at_or_below_entry():
    # Explicit tolerance=0.0 restores the strict "at or below ideal entry" rule
    cfg = SmartMoneyConfig(entry_price_tolerance=0.0)
    tf = TradingFilter(cfg, TraderConfig())
    below = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 99.0},
    )
    above = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 101.0},
    )
    assert len(below) == 1
    assert above == []


def test_entry_signal_skipped_when_already_held(tf):
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols={"AAPL"},
        current_prices={"AAPL": 90.0},
    )
    assert signals == []


def test_entry_signal_skipped_when_no_price_available(tf):
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={},
    )
    assert signals == []


def test_entry_signal_skipped_when_bypass_disabled():
    cfg = SmartMoneyConfig(
        overlap_weight=0.40,
        holding_weight_weight=0.30,
        performance_weight=0.30,
        momentum_weight=0.0,
        relative_strength_weight=0.0,
        entry_price_bypass_enabled=False,
    )
    tf = TradingFilter(cfg, TraderConfig())
    signals = tf.generate_entry_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        held_symbols=set(),
        current_prices={"AAPL": 90.0},
    )
    assert signals == []


# ---------------------------------------------------------------- Path B

def test_scanner_candidate_in_top_n_passes(tf):
    outcomes = tf.filter_scanner_candidates(
        candidates=[_candidate("AAPL")],
        portfolio_symbols={"AAPL", "ZZZ"},
        top_n_symbols={"AAPL"},
    )
    assert len(outcomes) == 1
    assert outcomes[0].kept is True
    assert outcomes[0].reason == "passed"


def test_scanner_candidate_in_portfolio_not_top_n_dropped(tf):
    outcomes = tf.filter_scanner_candidates(
        candidates=[_candidate("ZZZ")],
        portfolio_symbols={"AAPL", "ZZZ"},
        top_n_symbols={"AAPL"},
    )
    assert outcomes[0].kept is False
    assert outcomes[0].reason == "not-in-top-n"


def test_scanner_candidate_not_in_portfolio_dropped(tf):
    outcomes = tf.filter_scanner_candidates(
        candidates=[_candidate("WXYZ")],
        portfolio_symbols={"AAPL"},
        top_n_symbols={"AAPL"},
    )
    assert outcomes[0].kept is False
    assert outcomes[0].reason == "not-in-portfolio"


# ---------------------------------------------------------------- Overlap

def test_overlap_fires_when_both_paths_trigger(tf):
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[_candidate("AAPL")],
        held_symbols=set(),
        current_prices={"AAPL": 99.0},
        min_conviction_score=5.0,
    )
    assert len(signals) == 1
    sig = signals[0]
    assert sig.strategy_name == "SmartMoneyPortfolio_Overlap"
    assert sig.metadata["overlap_boost"] is True
    assert sig.metadata["risk_multiplier"] == 2.0
    assert sig.metadata["scanner_sources"] == ["capitol_trades"]
    assert sig.entry_price == 99.0


def test_overlap_skipped_when_only_path_a_fires(tf):
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[],                       # no scanner hit
        held_symbols=set(),
        current_prices={"AAPL": 99.0},
        min_conviction_score=5.0,
    )
    assert signals == []


def test_overlap_skipped_when_only_path_b_fires(tf):
    # 110 is above the default 5% tolerance band (100 * 1.05 = 105)
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[_candidate("AAPL")],
        held_symbols=set(),
        current_prices={"AAPL": 110.0},
        min_conviction_score=5.0,
    )
    assert signals == []


def test_overlap_fires_within_tolerance_band(tf):
    # 103 is within the default 5% tolerance of entry=100 → overlap triggers
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[_candidate("AAPL")],
        held_symbols=set(),
        current_prices={"AAPL": 103.0},
        min_conviction_score=5.0,
    )
    assert len(signals) == 1
    assert signals[0].metadata["overlap_boost"] is True
    assert signals[0].entry_price == 103.0


def test_overlap_skipped_when_conviction_below_threshold(tf):
    low_conviction = CandidateSymbol(
        symbol="AAPL", conviction_score=2.0, sources=["capitol_trades"],
        actors=["X"], total_dollar_volume=100_000.0, filing_count=1,
        most_recent_filing=datetime.now(),
    )
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[low_conviction],
        held_symbols=set(),
        current_prices={"AAPL": 99.0},
        min_conviction_score=5.0,
    )
    assert signals == []


def test_overlap_skipped_when_held(tf):
    signals = tf.generate_overlap_signals(
        top_n=[_top_n("AAPL", entry=100.0)],
        scanner_candidates=[_candidate("AAPL")],
        held_symbols={"AAPL"},
        current_prices={"AAPL": 99.0},
        min_conviction_score=5.0,
    )
    assert signals == []


# ---------------------------------------------------------------- is_tradable

def _stock(overlap: int, max_wgt: float) -> TopNStock:
    return TopNStock(
        symbol="TEST",
        composite_score=0.3,
        optimal_entry_price=100.0,
        overlap_count=overlap,
        sources=["provider_a"],
        max_single_fund_weight=max_wgt,
    )


def test_is_tradable_passes_when_overlap_meets_minimum():
    assert is_tradable(_stock(overlap=2, max_wgt=0.01), min_overlap_count=2, concentration_exception=0.15)
    assert is_tradable(_stock(overlap=5, max_wgt=0.0), min_overlap_count=2, concentration_exception=0.15)


def test_is_tradable_blocks_low_overlap_low_weight():
    # ESLT profile: 1 fund at 1.6% weight
    assert not is_tradable(_stock(overlap=1, max_wgt=0.016),
                           min_overlap_count=2, concentration_exception=0.15)


def test_is_tradable_concentration_exception_rescues_aapl_profile():
    # AAPL profile: 1 fund (Berkshire) at 22.6% weight — should still trade
    assert is_tradable(_stock(overlap=1, max_wgt=0.226),
                       min_overlap_count=2, concentration_exception=0.15)


def test_is_tradable_exact_threshold_passes():
    # A single-fund holding at exactly 15% should pass (>= threshold)
    assert is_tradable(_stock(overlap=1, max_wgt=0.15),
                       min_overlap_count=2, concentration_exception=0.15)


def test_is_tradable_min_overlap_1_disables_the_rule():
    # Setting min_overlap_count=1 means every single-fund pick qualifies
    assert is_tradable(_stock(overlap=1, max_wgt=0.001),
                       min_overlap_count=1, concentration_exception=0.15)


def test_is_tradable_defaults_preserve_pre_existing_topnstock_ctor():
    # Old call sites that didn't pass max_single_fund_weight get default 0.0;
    # the rule should gracefully block those unless overlap alone qualifies.
    stock = TopNStock(symbol="X", composite_score=0.3, optimal_entry_price=100.0,
                     overlap_count=1, sources=[])
    assert not is_tradable(stock, min_overlap_count=2, concentration_exception=0.15)
    assert is_tradable(stock, min_overlap_count=1, concentration_exception=0.15)
