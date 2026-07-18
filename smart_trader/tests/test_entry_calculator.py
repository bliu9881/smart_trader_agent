"""Tests for EntryCalculator."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict

import pandas as pd

from smart_trader.core.entry_calculator import EntryCalculator, TechnicalSignals
from smart_trader.settings.config import SmartMoneyConfig


class StubOHLCV:
    def __init__(self, bars_by_symbol: Dict[str, pd.DataFrame]):
        self._bars = bars_by_symbol

    def get_or_fetch(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return self._bars.get(symbol.upper(), pd.DataFrame())


def _bars(prices, volumes=None):
    end = datetime.now()
    idx = pd.date_range(end=end, periods=len(prices), freq="D")
    if volumes is None:
        volumes = [1_000_000] * len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": volumes,
    }, index=idx)


def test_returns_none_for_missing_symbol():
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({}))
    out = calc.compute(["XYZ"])
    assert out == {"XYZ": None}


def test_entry_is_min_of_low_and_vwap():
    # Flat prices → low == close; VWAP == close → entry ~= 0.99 * close (the low)
    prices = [100.0] * 25
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"AAPL": _bars(prices)}))
    out = calc.compute(["AAPL"])
    entry = out["AAPL"]
    assert entry is not None
    # Low of 99.00 is below VWAP of ~100, so entry == 99.00
    assert abs(entry - 99.0) < 0.01


def test_vwap_reflects_volume_weighting():
    # High-volume bar at low price should drag VWAP below flat-close average.
    prices = [100.0] * 19 + [80.0]           # last bar much cheaper
    volumes = [100_000] * 19 + [10_000_000]  # and high volume
    bars = _bars(prices, volumes)
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"AAPL": bars}))
    out = calc.compute(["AAPL"])
    entry = out["AAPL"]
    assert entry is not None
    # VWAP is heavily weighted toward the 80-level; entry should be below 80
    assert entry < 82.0


def test_compute_handles_multiple_symbols_independently():
    bars_a = _bars([100.0] * 25)
    bars_b = _bars([50.0] * 25)
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"A": bars_a, "B": bars_b}))
    out = calc.compute(["A", "B", "C"])
    assert out["A"] is not None
    assert out["B"] is not None
    assert out["C"] is None
    assert out["A"] > out["B"]


# ---------------------------------------------------------------------------
# compute_technical() tests — Humbled Trader primary strategy
# ---------------------------------------------------------------------------

def _tech_bars(prices, opens=None, highs=None, lows=None):
    """Build a DataFrame with enough bars for 200-SMA computation."""
    n = len(prices)
    end = datetime.now()
    idx = pd.date_range(end=end, periods=n, freq="D")
    if opens is None:
        opens = prices
    if highs is None:
        highs = [p * 1.01 for p in prices]
    if lows is None:
        lows = [p * 0.99 for p in prices]
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": [1_000_000] * n,
    }, index=idx)


def _cfg(**kwargs):
    """Return SmartMoneyConfig with overrides."""
    cfg = SmartMoneyConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def test_technical_price_equal_to_sma_is_not_above():
    """Price exactly equal to 200 SMA is NOT considered above (strict >)."""
    # 210 bars all at 100 → 200 SMA = 100, price = 100; strict > means False
    prices = [100.0] * 210
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"AAPL": _tech_bars(prices)}))
    out = calc.compute_technical(["AAPL"], {}, SmartMoneyConfig())
    ts = out["AAPL"]
    assert not ts.above_200_sma      # price == SMA, strict > is False
    # Pullback may still be detected (price ≤ ema_8 * 1.02), but trend filter
    # in TradingFilter will block entry since above_200_sma is False.
    assert ts.current_price == 100.0


def test_technical_above_200_sma_with_pullback():
    """Price slightly above 200 SMA and touching 8 EMA → ema_pullback signal."""
    # First 200 bars at 90, last 10 bars at 101 → 200 SMA ≈ 90.55, price=101 > SMA ✓
    # 8 EMA of all bars will be close to 101 (recent bars dominate). Price at ema_8
    # level (within tolerance) → pullback.
    prices = [90.0] * 200 + [101.0] * 10
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"AAPL": _tech_bars(prices)}))
    cfg = _cfg(ema_tolerance=0.02)
    out = calc.compute_technical(["AAPL"], {}, cfg)
    ts = out["AAPL"]
    assert ts.above_200_sma, f"expected above 200 SMA, sma would be ~90.55, price=101"
    assert ts.current_price is not None
    # EMA is near 101, price is 101 which is ≤ ema_8 * 1.02 → pullback fires
    assert ts.is_ema_pullback
    assert ts.signal_type in ("ema_pullback", "both")


def test_technical_below_200_sma_no_signal():
    """Price below 200 SMA: above_200_sma=False, signal_type may still compute."""
    # Price drops from 100 to 50 → SMA ≈ 99.76, price=50 < SMA
    prices = [100.0] * 209 + [50.0]
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"AAPL": _tech_bars(prices)}))
    out = calc.compute_technical(["AAPL"], {}, SmartMoneyConfig())
    ts = out["AAPL"]
    assert not ts.above_200_sma


def test_technical_gap_up_detected():
    """Gap up: today's open > prev close * 1.03 AND > prev high."""
    base = [100.0] * 209
    # prev bar: close=100, high=101; today: open=105 (5% gap, above prev high 101)
    opens = base + [105.0]
    highs = [p * 1.01 for p in base] + [107.0]
    lows = [p * 0.99 for p in base] + [104.0]
    closes = base + [106.0]
    bars = _tech_bars(closes, opens=opens, highs=highs, lows=lows)
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"TSLA": bars}))
    out = calc.compute_technical(["TSLA"], {}, SmartMoneyConfig())
    ts = out["TSLA"]
    assert ts.is_gap_up
    assert ts.signal_type in ("gap_up", "both")


def test_technical_no_gap_when_gap_too_small():
    """A sub-threshold gap (1%) does not trigger gap_up."""
    base = [100.0] * 209
    opens = base + [101.0]   # only 1% gap, below default 3% threshold
    highs = [p * 1.01 for p in base] + [102.0]
    lows = [p * 0.99 for p in base] + [100.5]
    closes = base + [101.5]
    bars = _tech_bars(closes, opens=opens, highs=highs, lows=lows)
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"TSLA": bars}))
    out = calc.compute_technical(["TSLA"], {}, SmartMoneyConfig())
    ts = out["TSLA"]
    assert not ts.is_gap_up


def test_technical_fetch_failure_returns_fail_open():
    """When OHLCV fetch fails, result is fail-open: above_200_sma=True, no signals."""
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({}))
    out = calc.compute_technical(["MISSING"], {}, SmartMoneyConfig())
    ts = out["MISSING"]
    assert ts.above_200_sma          # fail-open, not blocked
    assert not ts.is_ema_pullback
    assert not ts.is_gap_up
    assert ts.signal_type == "none"


def test_technical_insufficient_bars_returns_fail_open():
    """Fewer than 200 bars yields fail-open (can't compute 200 SMA)."""
    prices = [100.0] * 50   # only 50 bars, need 200
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"X": _tech_bars(prices)}))
    out = calc.compute_technical(["X"], {}, SmartMoneyConfig())
    ts = out["X"]
    assert ts.signal_type == "none"


def test_technical_price_from_bars_when_not_supplied():
    """When current_prices dict doesn't include a symbol, bars.iloc[-1]['close'] is used."""
    prices = [90.0] * 200 + [110.0] * 10
    calc = EntryCalculator(SmartMoneyConfig(), StubOHLCV({"Z": _tech_bars(prices)}))
    out = calc.compute_technical(["Z"], {}, SmartMoneyConfig())  # empty prices dict
    ts = out["Z"]
    assert ts.current_price == 110.0
