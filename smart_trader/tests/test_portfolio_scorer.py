"""Tests for PortfolioScorer."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytest

from smart_trader.core.portfolio_scorer import (
    PortfolioScorer,
    ScoredStock,
    _apply_momentum_tiebreak,
    _rank_normalize,
)
from smart_trader.core.smart_money import FundHoldings
from smart_trader.settings.config import SmartMoneyConfig


class StubOHLCV:
    """Minimal OHLCVStore stub that returns predetermined DataFrames."""

    def __init__(self, bars_by_symbol: Dict[str, pd.DataFrame]):
        self._bars = bars_by_symbol

    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return self._bars.get(symbol.upper(), pd.DataFrame())


def _make_bars(start_price: float, end_price: float, days: int = 220) -> pd.DataFrame:
    """Build a linearly interpolated daily OHLCV DataFrame."""
    end = datetime.now()
    start = end - timedelta(days=days)
    idx = pd.date_range(start=start, end=end, freq="D")
    n = len(idx)
    closes = [start_price + (end_price - start_price) * i / (n - 1) for i in range(n)]
    df = pd.DataFrame({
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)
    return df


def _holding(provider: str, fund: str, symbol: str, weight: float) -> FundHoldings:
    return FundHoldings(
        fund_name=fund,
        provider_name=provider,
        symbol=symbol,
        share_count=1000,
        holding_weight=weight,
        market_value=100_000.0,
        as_of_date=datetime.now(),
    )


# ---------------------------------------------------------------- rank_normalize

def test_rank_normalize_missing_values_score_zero():
    raw = {"A": 0.1, "B": None, "C": 0.5}
    out = _rank_normalize(raw)
    assert out["B"] == 0.0
    assert out["A"] == 0.0   # lowest rank
    assert out["C"] == 1.0   # highest rank


def test_rank_normalize_single_value():
    out = _rank_normalize({"A": 0.3})
    assert out["A"] == 0.5


def test_rank_normalize_all_missing():
    out = _rank_normalize({"A": None, "B": None})
    assert out == {"A": 0.0, "B": 0.0}


# ---------------------------------------------------------------- tiebreak

def test_momentum_tiebreak_reorders_close_scores():
    stocks = [
        ScoredStock("A", 0.80, 1, 0.1, 0.0, 0.2, 0.0),
        ScoredStock("B", 0.79, 1, 0.1, 0.0, 0.9, 0.0),  # within 5% → higher momentum
        ScoredStock("C", 0.50, 1, 0.1, 0.0, 0.5, 0.0),  # well below cluster
    ]
    _apply_momentum_tiebreak(stocks, epsilon=0.05)
    assert [s.symbol for s in stocks] == ["B", "A", "C"]


def test_momentum_tiebreak_preserves_order_when_scores_differ():
    stocks = [
        ScoredStock("A", 0.90, 1, 0.1, 0.0, 0.1, 0.0),
        ScoredStock("B", 0.50, 1, 0.1, 0.0, 0.9, 0.0),
    ]
    _apply_momentum_tiebreak(stocks, epsilon=0.05)
    assert [s.symbol for s in stocks] == ["A", "B"]


# ---------------------------------------------------------------- PortfolioScorer.score

def test_score_includes_every_stock_held_by_at_least_one_fund():
    """Req 2.1: no minimum-overlap gate."""
    scorer = PortfolioScorer(SmartMoneyConfig(), StubOHLCV({}))
    holdings = [
        _holding("berkshire_13f", "Berkshire Hathaway", "AAPL", 0.40),
        _holding("ark_invest", "ARK ARKK", "ZZZ", 0.01),  # only one fund, tiny weight
    ]
    scored = scorer.score(holdings, n_providers=5)
    symbols = {s.symbol for s in scored}
    assert "AAPL" in symbols
    assert "ZZZ" in symbols


def test_score_assigns_zero_performance_when_no_history():
    scorer = PortfolioScorer(SmartMoneyConfig(), StubOHLCV({}))
    holdings = [_holding("berkshire_13f", "Berkshire Hathaway", "AAPL", 0.40)]
    scored = scorer.score(holdings, n_providers=5)
    aapl = next(s for s in scored if s.symbol == "AAPL")
    assert aapl.performance_score == 0.0
    assert aapl.momentum_score == 0.0
    assert aapl.relative_strength == 0.0


def test_composite_score_matches_weighted_formula():
    cfg = SmartMoneyConfig()  # defaults: overlap 0.60, hw 0.30, perf 0.10, mom 0, rs 0
    scorer = PortfolioScorer(cfg, StubOHLCV({}))
    holdings = [
        _holding("berkshire_13f", "Berkshire Hathaway", "AAPL", 0.40),
        _holding("ark_invest", "ARK ARKK", "AAPL", 0.20),
    ]
    scored = scorer.score(holdings, n_providers=5)
    aapl = next(s for s in scored if s.symbol == "AAPL")
    # overlap 2/5 = 0.4, avg weight (0.40+0.20)/2 = 0.30, performance 0
    expected = 0.60 * 0.4 + 0.30 * 0.30 + 0.10 * 0.0
    assert abs(aapl.composite_score - expected) < 1e-6


def test_score_uses_rank_normalization_for_performance():
    cfg = SmartMoneyConfig()
    bars = {
        "SPY": _make_bars(400, 440),       # +10%
        "AAA": _make_bars(100, 130),       # +30%
        "BBB": _make_bars(100, 110),       # +10%
        "CCC": _make_bars(100, 105),       # +5%
    }
    scorer = PortfolioScorer(cfg, StubOHLCV(bars))
    holdings = [
        _holding("berkshire_13f", "Berkshire", "AAA", 0.1),
        _holding("berkshire_13f", "Berkshire", "BBB", 0.1),
        _holding("berkshire_13f", "Berkshire", "CCC", 0.1),
    ]
    scored = scorer.score(holdings, n_providers=5)
    by_sym = {s.symbol: s for s in scored}
    assert by_sym["AAA"].performance_score > by_sym["BBB"].performance_score
    assert by_sym["BBB"].performance_score > by_sym["CCC"].performance_score
    # Cross-sectional rank normalization → min is 0, max is 1
    assert by_sym["AAA"].performance_score == 1.0
    assert by_sym["CCC"].performance_score == 0.0


def test_score_sorted_descending():
    scorer = PortfolioScorer(SmartMoneyConfig(), StubOHLCV({}))
    holdings = [
        _holding("berkshire_13f", "Berkshire", "HI", 0.50),
        _holding("ark_invest", "ARK", "HI", 0.40),
        _holding("berkshire_13f", "Berkshire", "LO", 0.01),
    ]
    scored = scorer.score(holdings, n_providers=5)
    assert scored[0].symbol == "HI"
    assert scored[0].composite_score >= scored[-1].composite_score


# ---------------------------------------------------------------- config validation

def test_config_validates_weight_sum():
    with pytest.raises(ValueError, match="weights must sum"):
        SmartMoneyConfig(
            overlap_weight=0.5,
            holding_weight_weight=0.3,
            performance_weight=0.3,
            momentum_weight=0.0,
            relative_strength_weight=0.0,
        )


def test_config_default_weights_sum_to_one():
    SmartMoneyConfig()  # Should not raise
