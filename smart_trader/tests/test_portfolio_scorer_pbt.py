"""Property-based tests for PortfolioScorer.

Tests correctness properties from the design document:
  - Property 1: Composite_Score bounded [0, 1]
  - Property 2: Scorer output preserves universe size
  - Property 3: Scorer output sorted descending
  - Property 5: Rank normalization output in [0, 1]
  - Property 11: Scoring weight validation rejects invalid sums
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from smart_trader.core.portfolio_scorer import (
    PortfolioScorer,
    _apply_momentum_tiebreak,
    _rank_normalize,
)
from smart_trader.core.smart_money import FundHoldings
from smart_trader.settings.config import SmartMoneyConfig


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate valid ticker symbols (1-5 uppercase letters)
ticker_st = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=5
)

# Generate a single FundHoldings record
def fund_holdings_st(symbol_st=None):
    if symbol_st is None:
        symbol_st = ticker_st
    return st.builds(
        FundHoldings,
        fund_name=st.text(min_size=1, max_size=30),
        provider_name=st.sampled_from([
            "berkshire_13f", "ark_invest", "pershing_square_13f",
            "appaloosa_13f", "duquesne_13f",
        ]),
        symbol=symbol_st,
        share_count=st.integers(min_value=1, max_value=10_000_000),
        holding_weight=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        market_value=st.floats(min_value=1.0, max_value=1e12, allow_nan=False),
        as_of_date=st.just(datetime.now()),
    )


# Generate a list of holdings with at least one entry
holdings_list_st = st.lists(fund_holdings_st(), min_size=1, max_size=30)

# Generate valid scoring weights (5 non-negative floats summing to 1.0)
def valid_weights_st():
    """Generate 5 non-negative floats that sum to 1.0."""
    return st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=5, max_size=5,
    ).map(_normalize_weights).filter(lambda w: w is not None)


def _normalize_weights(raw):
    total = sum(raw)
    if total <= 0:
        return None
    return [x / total for x in raw]


class StubOHLCV:
    """Minimal OHLCVStore stub returning empty DataFrames."""
    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()


class PriceStubOHLCV:
    """OHLCVStore stub returning linearly interpolated bars for any symbol."""
    def __init__(self, base_price: float = 100.0, return_pct: float = 0.1):
        self.base_price = base_price
        self.return_pct = return_pct

    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=220)
        idx = pd.date_range(start=start_dt, end=end_dt, freq="D")
        n = len(idx)
        start_price = self.base_price
        end_price = self.base_price * (1 + self.return_pct)
        closes = [start_price + (end_price - start_price) * i / (n - 1) for i in range(n)]
        return pd.DataFrame({
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }, index=idx)


# ---------------------------------------------------------------------------
# Property 1: Composite_Score is bounded [0, 1]
# ---------------------------------------------------------------------------

@given(
    holdings=holdings_list_st,
    weights=valid_weights_st(),
)
@settings(max_examples=100, deadline=None)
def test_composite_score_bounded_zero_one(holdings, weights):
    """Every Composite_Score must be in [0.0, 1.0] for any valid weights."""
    config = SmartMoneyConfig(
        overlap_weight=weights[0],
        holding_weight_weight=weights[1],
        performance_weight=weights[2],
        momentum_weight=weights[3],
        relative_strength_weight=weights[4],
    )
    scorer = PortfolioScorer(config, StubOHLCV())
    n_providers = max(1, len({h.provider_name for h in holdings}))
    scored = scorer.score(holdings, n_providers=n_providers)

    for s in scored:
        assert 0.0 <= s.composite_score <= 1.0 + 1e-9, (
            f"{s.symbol}: composite_score={s.composite_score} out of [0, 1]"
        )


# ---------------------------------------------------------------------------
# Property 2: Scorer output preserves universe size
# ---------------------------------------------------------------------------

@given(holdings=holdings_list_st)
@settings(max_examples=100, deadline=None)
def test_scorer_preserves_universe_size(holdings):
    """Number of scored stocks == number of unique symbols in input."""
    config = SmartMoneyConfig()
    scorer = PortfolioScorer(config, StubOHLCV())
    n_providers = max(1, len({h.provider_name for h in holdings}))
    scored = scorer.score(holdings, n_providers=n_providers)

    unique_symbols = {h.symbol for h in holdings}
    assert len(scored) == len(unique_symbols), (
        f"Expected {len(unique_symbols)} scored stocks, got {len(scored)}"
    )


# ---------------------------------------------------------------------------
# Property 3: Scorer output is sorted descending
# ---------------------------------------------------------------------------

@given(holdings=holdings_list_st)
@settings(max_examples=100, deadline=None)
def test_scorer_output_sorted_descending(holdings):
    """Output must be sorted by composite_score in non-increasing order."""
    config = SmartMoneyConfig()
    scorer = PortfolioScorer(config, StubOHLCV())
    n_providers = max(1, len({h.provider_name for h in holdings}))
    scored = scorer.score(holdings, n_providers=n_providers)

    for i in range(len(scored) - 1):
        assert scored[i].composite_score >= scored[i + 1].composite_score - 1e-9, (
            f"Not sorted at index {i}: "
            f"{scored[i].symbol}={scored[i].composite_score} < "
            f"{scored[i+1].symbol}={scored[i+1].composite_score}"
        )


# ---------------------------------------------------------------------------
# Property 5: Rank normalization output is in [0, 1]
# ---------------------------------------------------------------------------

@given(
    raw=st.dictionaries(
        keys=ticker_st,
        values=st.one_of(
            st.none(),
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=50,
    )
)
@settings(max_examples=200, deadline=None)
def test_rank_normalize_output_in_zero_one(raw):
    """All rank-normalized values must be in [0.0, 1.0]. None maps to 0.0."""
    result = _rank_normalize(raw)

    assert set(result.keys()) == set(raw.keys()), "Keys must be preserved"

    for key, val in result.items():
        assert 0.0 <= val <= 1.0, f"rank_normalize[{key}] = {val} out of [0, 1]"

    # None inputs must map to 0.0
    for key, raw_val in raw.items():
        if raw_val is None:
            assert result[key] == 0.0, f"None input {key} should map to 0.0, got {result[key]}"


# ---------------------------------------------------------------------------
# Property 11: Scoring weight validation rejects invalid sums
# ---------------------------------------------------------------------------

@given(
    weights=st.lists(
        st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
        min_size=5, max_size=5,
    )
)
@settings(max_examples=200, deadline=None)
def test_weight_validation_rejects_invalid_sums(weights):
    """SmartMoneyConfig raises ValueError iff weights don't sum to 1.0."""
    weight_sum = sum(weights)
    should_raise = abs(weight_sum - 1.0) > 1e-6

    if should_raise:
        with pytest.raises(ValueError, match="weights must sum"):
            SmartMoneyConfig(
                overlap_weight=weights[0],
                holding_weight_weight=weights[1],
                performance_weight=weights[2],
                momentum_weight=weights[3],
                relative_strength_weight=weights[4],
            )
    else:
        # Should not raise
        SmartMoneyConfig(
            overlap_weight=weights[0],
            holding_weight_weight=weights[1],
            performance_weight=weights[2],
            momentum_weight=weights[3],
            relative_strength_weight=weights[4],
        )
