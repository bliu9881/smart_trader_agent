"""Property-based tests for EntryCalculator.

Tests correctness property from the design document:
  - Property 6: Entry price is conservative (≤ both 20-day low and 20-day VWAP)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from smart_trader.core.entry_calculator import EntryCalculator
from smart_trader.settings.config import SmartMoneyConfig


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

@st.composite
def ohlcv_bars_st(draw):
    """Generate a valid OHLCV DataFrame with at least 25 bars.

    Each bar has positive prices where high >= close >= low and high >= open >= low,
    and positive volume.
    """
    n_bars = draw(st.integers(min_value=25, max_value=60))
    end = datetime.now()
    idx = pd.date_range(end=end, periods=n_bars, freq="D")

    closes = []
    for _ in range(n_bars):
        c = draw(st.floats(min_value=1.0, max_value=10000.0, allow_nan=False, allow_infinity=False))
        closes.append(c)

    opens = []
    highs = []
    lows = []
    volumes = []
    for c in closes:
        # Generate spread around close
        spread = draw(st.floats(min_value=0.001, max_value=0.10, allow_nan=False, allow_infinity=False))
        low = c * (1 - spread)
        high = c * (1 + spread)
        o = draw(st.floats(min_value=low, max_value=high, allow_nan=False, allow_infinity=False))
        v = draw(st.integers(min_value=100, max_value=100_000_000))
        opens.append(o)
        highs.append(high)
        lows.append(low)
        volumes.append(v)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=idx)
    return df


class StubOHLCV:
    """OHLCVStore stub that returns a predetermined DataFrame for any symbol."""
    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return self._bars


# ---------------------------------------------------------------------------
# Property 6: Entry price is conservative (≤ both inputs)
# ---------------------------------------------------------------------------

@given(bars=ohlcv_bars_st())
@settings(max_examples=100, deadline=None)
def test_entry_price_leq_both_support_and_vwap(bars):
    """Optimal_Entry_Price must be ≤ 20-day low AND ≤ 20-day VWAP."""
    config = SmartMoneyConfig(
        entry_vwap_lookback_days=20,
        entry_support_lookback_days=20,
    )
    calc = EntryCalculator(config, StubOHLCV(bars))
    result = calc.compute(["TEST"])
    entry = result["TEST"]

    if entry is None:
        return  # No entry price computed (insufficient data) — skip

    # Independently compute the two components
    tail = bars.tail(20)
    support = float(tail["low"].min())
    typical = (tail["high"] + tail["low"] + tail["close"]) / 3.0
    volume = tail["volume"].astype(float)
    total_volume = float(volume.sum())

    if total_volume > 0:
        vwap = float((typical * volume).sum() / total_volume)
        assert entry <= vwap + 1e-6, (
            f"Entry {entry} > VWAP {vwap}"
        )

    assert entry <= support + 1e-6, (
        f"Entry {entry} > 20-day low {support}"
    )

    # Entry should equal min(support, vwap)
    if total_volume > 0:
        expected = min(support, vwap)
        assert abs(entry - expected) < 1e-6, (
            f"Entry {entry} != min(support={support}, vwap={vwap}) = {expected}"
        )
