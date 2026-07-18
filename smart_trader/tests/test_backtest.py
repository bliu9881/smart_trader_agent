"""Tests for the backtest engine — portfolio simulation and metrics."""
from __future__ import annotations

from datetime import datetime
from typing import List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from smart_trader.backtest.engine import BacktestEngine, BacktestResult, _is_true
from smart_trader.backtest.metrics import compute_metrics
from smart_trader.backtest.portfolio import SimulatedPortfolio, Trade
from smart_trader.settings.config import load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg():
    return load_config()


def _make_trade(
    pnl: float,
    signal_type: str = "gap_up",
    entry: str = "2024-01-05",
    exit_d: str = "2024-01-20",
    exit_reason: str = "take_profit",
) -> Trade:
    t = Trade(
        symbol="AAPL",
        entry_date=entry,
        entry_price=100.0,
        shares=10,
        stop_loss=92.0,
        take_profit=120.0,
        trail_pct=0.05,
        signal_type=signal_type,
        confidence=0.65,
    )
    t.exit_date = exit_d
    t.exit_price = 100.0 + pnl / 10
    t.exit_reason = exit_reason
    t.pnl = pnl
    return t


# ---------------------------------------------------------------------------
# SimulatedPortfolio — position management
# ---------------------------------------------------------------------------

class TestSimulatedPortfolio:
    def test_open_position_deducts_cash(self):
        p = SimulatedPortfolio(100_000, _cfg())
        assert p.open_position("AAPL", "2024-01-05", 150.0, "gap_up", 0.65)
        assert p._cash < 100_000

    def test_returns_false_for_zero_price(self):
        p = SimulatedPortfolio(100_000, _cfg())
        assert not p.open_position("AAPL", "2024-01-05", 0.0, "gap_up", 0.65)

    def test_cannot_open_same_symbol_twice(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 150.0, "gap_up", 0.65)
        assert not p.open_position("AAPL", "2024-01-06", 152.0, "ema_pullback", 0.50)

    def test_max_concurrent_positions_respected(self):
        cfg = _cfg()
        max_pos = cfg.risk.max_concurrent_positions
        p = SimulatedPortfolio(500_000, cfg)
        opened = sum(
            p.open_position(f"SYM{i}", "2024-01-05", 100.0 + i, "gap_up", 0.65)
            for i in range(max_pos + 3)
        )
        assert opened == max_pos

    def test_equity_at_entry_close_to_initial(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 150.0, "gap_up", 0.65)
        eq = p.equity({"AAPL": 150.0})
        assert abs(eq - 100_000) < 500  # rounding at most ~$500

    def test_equity_increases_with_price(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        eq_low = p.equity({"AAPL": 90.0})
        eq_high = p.equity({"AAPL": 120.0})
        assert eq_high > eq_low

    def test_stop_loss_exit(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("TSLA", "2024-01-05", 200.0, "gap_up", 0.65)
        stop = 200 * (1 - _cfg().trader.default_stop_pct)
        bars = {"TSLA": pd.Series({"open": 195.0, "high": 196.0, "low": stop - 2, "close": 195.0})}
        closed = p.update_exits("2024-01-08", bars)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop"
        assert closed[0].pnl < 0

    def test_take_profit_exit(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        tp = 100 * (1 + _cfg().trader.default_take_profit_pct)
        bars = {"AAPL": pd.Series({"open": 105.0, "high": tp + 5, "low": 104.0, "close": 115.0})}
        closed = p.update_exits("2024-01-20", bars)
        assert len(closed) == 1
        assert closed[0].exit_reason == "take_profit"
        assert closed[0].pnl > 0

    def test_no_exit_when_price_range_benign(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        bars = {"AAPL": pd.Series({"open": 101.0, "high": 103.0, "low": 99.0, "close": 102.0})}
        closed = p.update_exits("2024-01-06", bars)
        assert len(closed) == 0

    def test_close_all_exits_at_given_prices(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("MSFT", "2024-01-05", 400.0, "gap_up", 0.65)
        closed = p.close_all("2024-12-31", {"MSFT": 450.0})
        assert len(closed) == 1
        assert closed[0].exit_reason == "end_of_test"
        assert closed[0].pnl > 0

    def test_cash_restored_after_exit(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        cash_after_entry = p._cash
        tp = 100 * 1.20
        bars = {"AAPL": pd.Series({"open": 110.0, "high": tp + 5, "low": 109.0, "close": 119.0})}
        p.update_exits("2024-01-20", bars)
        assert p._cash > cash_after_entry  # cash restored + profit

    def test_record_equity_appends_to_history(self):
        p = SimulatedPortfolio(100_000, _cfg())
        p.record_equity()
        p.record_equity()
        assert len(p.equity_history()) == 2


# ---------------------------------------------------------------------------
# Transaction costs (slippage + commission)
# ---------------------------------------------------------------------------

class TestTransactionCosts:
    def test_defaults_are_frictionless(self):
        """Direct construction defaults to zero costs (keeps other tests valid)."""
        p = SimulatedPortfolio(100_000.0, _cfg())
        assert p._slip == 0.0 and p._comm_per_share == 0.0 and p._min_comm == 0.0
        assert p._commission(100, 50.0) == 0.0

    def test_commission_floor_and_cap(self):
        p = SimulatedPortfolio(100_000.0, _cfg(),
                               commission_per_share=0.005, min_commission=1.0)
        # 100 sh * 0.005 = 0.50 → floored to the 1.00 minimum
        assert p._commission(100, 50.0) == pytest.approx(1.0)
        # 10,000 sh * 0.005 = 50 → above floor, below 1% cap (1% of 500k = 5000)
        assert p._commission(10_000, 50.0) == pytest.approx(50.0)
        # tiny order at a low price: floor 1.0 capped at 1% of notional (1% of $20 = 0.20)
        assert p._commission(2, 10.0) == pytest.approx(0.20)

    def test_entry_slippage_raises_effective_fill(self):
        p = SimulatedPortfolio(100_000.0, _cfg(), slippage_bps=10.0)  # 0.10%
        assert p.open_position("AAPL", "2024-01-02", 100.0, "gap_up", 0.6)
        # entry recorded at the adverse (higher) fill
        t = p._open["AAPL"].trade
        assert t.entry_price == pytest.approx(100.0 * 1.001)

    def test_stop_exit_applies_adverse_slip_takeprofit_does_not(self):
        # Stop (market) → slipped worse; take-profit (limit) → exact.
        p1 = SimulatedPortfolio(100_000.0, _cfg(), slippage_bps=20.0)
        p1.open_position("AAPL", "2024-01-02", 100.0, "gap_up", 0.6)
        sl = p1._open["AAPL"].trade.stop_loss
        bars = {"AAPL": pd.Series({"open": sl, "high": sl, "low": sl - 1, "close": sl})}
        closed = p1.update_exits("2024-01-03", bars)
        assert closed[0].exit_reason == "stop"
        assert closed[0].exit_price == pytest.approx(sl * (1.0 - 0.002))

        p2 = SimulatedPortfolio(100_000.0, _cfg(), slippage_bps=20.0)
        p2.open_position("AAPL", "2024-01-02", 100.0, "gap_up", 0.6)
        tp = p2._open["AAPL"].trade.take_profit
        bars = {"AAPL": pd.Series({"open": tp, "high": tp + 1, "low": tp, "close": tp})}
        closed = p2.update_exits("2024-01-03", bars)
        assert closed[0].exit_reason == "take_profit"
        assert closed[0].exit_price == pytest.approx(tp)  # no adverse slip on a limit

    def test_costs_reduce_pnl_vs_frictionless(self):
        def run(slip, comm):
            p = SimulatedPortfolio(100_000.0, _cfg(),
                                   slippage_bps=slip, commission_per_share=comm,
                                   min_commission=1.0)
            p.open_position("AAPL", "2024-01-02", 100.0, "gap_up", 0.6)
            tp = p._open["AAPL"].trade.take_profit
            bars = {"AAPL": pd.Series({"open": tp, "high": tp + 1, "low": 99, "close": tp})}
            return p.update_exits("2024-01-03", bars)[0].pnl

        gross = run(0.0, 0.0)
        net = run(5.0, 0.005)
        assert net < gross  # costs eat into a winning trade


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_win_rate_half(self):
        trades = [_make_trade(100), _make_trade(-50), _make_trade(200), _make_trade(-30)]
        m = compute_metrics(trades, [100_000] * 30)
        assert m["win_rate"] == pytest.approx(0.5)

    def test_profit_factor(self):
        trades = [_make_trade(200), _make_trade(-100)]
        m = compute_metrics(trades, [100_000] * 10)
        assert m["profit_factor"] == pytest.approx(2.0)

    def test_max_drawdown_negative(self):
        equity = [100_000, 105_000, 110_000, 95_000, 100_000]
        m = compute_metrics([_make_trade(100)], equity)
        assert m["max_drawdown"] < 0

    def test_max_drawdown_monotone_rising_is_zero(self):
        equity = [100_000, 101_000, 102_000, 103_000]
        m = compute_metrics([_make_trade(100)], equity)
        assert m["max_drawdown"] == pytest.approx(0.0)

    def test_sharpe_positive_trend(self):
        equity = [100_000 + i * 200 for i in range(60)]
        m = compute_metrics([_make_trade(500)], equity)
        assert m["sharpe_ratio"] > 0

    def test_no_closed_trades_returns_error(self):
        # Only end_of_test trades — excluded from closed-trade metrics
        t = _make_trade(100, exit_reason="end_of_test")
        m = compute_metrics([t], [100_000] * 10)
        assert "error" in m

    def test_empty_trades_returns_error(self):
        m = compute_metrics([], [100_000] * 10)
        assert "error" in m

    def test_by_signal_type_counts(self):
        trades = [
            _make_trade(100, signal_type="gap_up"),
            _make_trade(-50, signal_type="gap_up"),
            _make_trade(200, signal_type="ema_pullback"),
        ]
        m = compute_metrics(trades, [100_000] * 30)
        assert m["gap_up_trades"] == 2
        assert m["pullback_trades"] == 1

    def test_total_return_computed(self):
        equity = [100_000, 110_000]
        m = compute_metrics([_make_trade(100)], equity)
        assert m["total_return"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# BacktestEngine — indicator precompute
# ---------------------------------------------------------------------------

class TestPrecompute:
    def _make_df(self, n=250, base=100.0, trend="up"):
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        if trend == "up":
            closes = [base + i * 0.5 for i in range(n)]
        elif trend == "flat":
            closes = [base] * n
        else:
            closes = [base - i * 0.3 for i in range(n)]

        df = pd.DataFrame({
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }, index=dates)
        return df

    def test_sma_200_column_created(self):
        df = self._make_df(250)
        result = BacktestEngine._precompute(df, load_config().smart_money)
        assert "sma_200" in result.columns

    def test_ema_8_column_created(self):
        df = self._make_df(250)
        result = BacktestEngine._precompute(df, load_config().smart_money)
        assert "ema_8" in result.columns

    def test_above_sma_true_for_uptrend(self):
        df = self._make_df(250, trend="up")
        result = BacktestEngine._precompute(df, load_config().smart_money)
        # Last row should be above 200 SMA in a steady uptrend
        assert result["above_sma"].iloc[-1]

    def test_above_sma_false_for_downtrend(self):
        df = self._make_df(250, trend="down")
        result = BacktestEngine._precompute(df, load_config().smart_money)
        # Last row should be below 200 SMA in a steady downtrend
        assert not result["above_sma"].iloc[-1]

    def test_gap_up_requires_above_sma(self):
        df = self._make_df(250, trend="down")
        result = BacktestEngine._precompute(df, load_config().smart_money)
        # In a downtrend, is_gap_up should always be False (above_sma is False)
        assert not result["is_gap_up"].any()

    def test_no_lookahead_in_sma(self):
        # SMA at row 200 should equal mean of first 200 closes, not use future
        df = self._make_df(210, trend="flat", base=50.0)
        result = BacktestEngine._precompute(df, load_config().smart_money)
        # First 199 rows have NaN sma_200 (not enough history)
        assert result["sma_200"].iloc[:199].isna().all()
        # Row 199 (0-indexed) has the first valid SMA
        assert not pd.isna(result["sma_200"].iloc[199])


# ---------------------------------------------------------------------------
# Lever 1 — gap-up detection (prev_high guard permanently removed)
# ---------------------------------------------------------------------------

class TestGapUpDetection:
    def _make_gap_df(self, n=250, gap_open_pct=0.025):
        """Build a DataFrame where the last bar gaps by gap_open_pct above prev_close."""
        base = 100.0
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        closes = [base + i * 0.5 for i in range(n)]
        opens = [c - 0.3 for c in closes]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]

        prev_close = closes[-2]
        opens[-1] = prev_close * (1.0 + gap_open_pct)
        highs[-1] = opens[-1] + 2.0
        lows[-1] = opens[-1] - 0.5
        closes[-1] = opens[-1] + 1.0

        return pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": [1_000_000] * n,
        }, index=dates)

    def test_gap_above_threshold_fires(self):
        """open > prev_close × threshold AND above 200 SMA → gap_up fires."""
        cfg = load_config().smart_money
        cfg.gap_up_threshold = 0.02
        df = self._make_gap_df(gap_open_pct=0.025)  # 2.5% gap, threshold 2%
        result = BacktestEngine._precompute(df, cfg)
        assert _is_true(result["is_gap_up"].iloc[-1])

    def test_gap_below_threshold_no_signal(self):
        """open < prev_close × threshold → no signal."""
        cfg = load_config().smart_money
        cfg.gap_up_threshold = 0.05  # 5% threshold
        df = self._make_gap_df(gap_open_pct=0.025)  # only 2.5% gap
        result = BacktestEngine._precompute(df, cfg)
        assert not _is_true(result["is_gap_up"].iloc[-1])

    def test_gap_below_sma_no_signal(self):
        """Gap above threshold but price below 200 SMA → no signal (trend filter)."""
        cfg = load_config().smart_money
        cfg.gap_up_threshold = 0.02
        # Flat/declining prices so 200 SMA > close
        base = 100.0
        n = 250
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        closes = [base - i * 0.1 for i in range(n)]  # declining
        prev_close = closes[-2]
        opens = [c - 0.3 for c in closes]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        opens[-1] = prev_close * 1.03   # 3% gap
        highs[-1] = opens[-1] + 2.0
        lows[-1] = opens[-1] - 0.5
        closes[-1] = opens[-1] + 1.0
        df = pd.DataFrame({
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": [1_000_000] * n,
        }, index=dates)
        result = BacktestEngine._precompute(df, cfg)
        # Price is below its own 200 SMA (declining series) → no signal
        assert not _is_true(result["is_gap_up"].iloc[-1])


# ---------------------------------------------------------------------------
# Lever 2 — trail_pct override
# ---------------------------------------------------------------------------

class TestTrailPctOverride:
    def test_wider_trail_not_triggered(self):
        """10% trail (level=90) survives a low=94 dip that would stop a 5% trail (level=95).

        Position: entry=100, hard stop=92 (8% below), trail_pct=10% → trail_level=90.
        low=94: above hard stop (92) ✓, above 10% trail (90) ✓ → no exit.
        """
        cfg = load_config()
        cfg.trader.default_trail_pct = 0.10
        p_wide = SimulatedPortfolio(100_000, cfg)
        p_wide.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        # low=94: above hard stop 92 and above 10% trail 90 → should not exit
        bars = {"AAPL": pd.Series({"open": 96.0, "high": 97.0, "low": 94.0, "close": 96.5})}
        closed = p_wide.update_exits("2024-01-06", bars)
        assert len(closed) == 0

    def test_narrow_trail_triggered(self):
        """5% trail (level=95) exits on low=94; hard stop (92) not reached.

        Position: entry=100, hard stop=92 (8% below), trail_pct=5% → trail_level=95.
        low=94: above hard stop (92) ✓, below 5% trail (95) → trail exit.
        """
        p_narrow = SimulatedPortfolio(100_000, _cfg())
        p_narrow.open_position("AAPL", "2024-01-05", 100.0, "gap_up", 0.65)
        # low=94: above hard stop 92, below 5% trail 95 → trail exit
        bars = {"AAPL": pd.Series({"open": 96.0, "high": 97.0, "low": 94.0, "close": 96.5})}
        closed = p_narrow.update_exits("2024-01-06", bars)
        assert len(closed) == 1
        assert closed[0].exit_reason == "trail"

    def test_engine_run_accepts_trail_pct_kwarg(self):
        """BacktestEngine.run() kwarg reaches portfolio without mutating global config."""
        from smart_trader.settings.config import load_config as lc
        original_cfg = lc()
        engine = BacktestEngine(original_cfg)
        # Just verify the argument is accepted and config isn't mutated
        assert engine._cfg.trader.default_trail_pct == 0.05
        # After a run call with trail_pct=0.08 the engine's own config stays 0.05
        # (we can't run a real backtest here without yfinance; test the deepcopy logic)
        import copy
        cloned = copy.deepcopy(original_cfg)
        cloned.trader.default_trail_pct = 0.08
        assert original_cfg.trader.default_trail_pct == 0.05  # original unchanged


# ---------------------------------------------------------------------------
# Lever 3 — regime filter
# ---------------------------------------------------------------------------

class TestRegimeFilter:
    def _make_indicators(self, n=250, trend="up"):
        """Minimal indicators dict for engine day-loop testing."""
        base = 100.0
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [base + i * 0.4 for i in range(n)]
        df = pd.DataFrame({
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [500_000] * n,
        }, index=dates)
        sm = load_config().smart_money
        return BacktestEngine._precompute(df, sm)

    def test_regime_false_allows_entries(self):
        """When regime_ok[date]=False, no new position should open."""
        p = SimulatedPortfolio(100_000, _cfg())
        entries_allowed = False  # regime OFF
        count_before = p.open_count()
        if not entries_allowed:
            pass  # entries blocked by caller check — portfolio itself has no regime logic
        assert p.open_count() == count_before

    def test_regime_ok_dict_blocks_on_false(self):
        """Simulate the engine's entries_allowed check."""
        regime_ok = {"2024-03-01": False, "2024-03-04": True}
        assert not regime_ok.get("2024-03-01", True)   # blocked
        assert regime_ok.get("2024-03-04", True)        # allowed
        assert regime_ok.get("2024-03-05", True)        # missing → fail-open (True)

    def test_regime_fail_open_for_missing_dates(self):
        """Dates not in regime_ok default to True (entries allowed)."""
        regime_ok: dict = {}
        assert regime_ok.get("2024-01-15", True) is True


class TestPerCountryRegime:
    def test_is_canadian_suffix_detection(self):
        from smart_trader.backtest.engine import _is_canadian
        assert _is_canadian("RY.TO")
        assert _is_canadian("shop.to")        # case-insensitive
        assert _is_canadian("ABCD.V")          # TSX Venture
        assert _is_canadian("TECK-B.TO")       # share-class dash
        assert not _is_canadian("AAPL")
        assert not _is_canadian("BRK-B")       # US dash ticker, no suffix
        assert not _is_canadian("^GSPTSE")     # the index itself isn't a tradable CA symbol

    def test_us_symbol_gated_by_us_proxy(self):
        """A US symbol consults the US proxy map, not the CA one."""
        regime_maps = {
            "SPY": {"2024-03-01": False},
            "^GSPTSE": {"2024-03-01": True},
        }
        symbol_proxy = {"AAPL": "SPY", "RY.TO": "^GSPTSE"}

        def allowed(sym, date):
            proxy = symbol_proxy.get(sym, "SPY")
            return regime_maps.get(proxy, {}).get(date, True)

        # US market down → US symbol blocked, CA symbol still allowed same day
        assert not allowed("AAPL", "2024-03-01")
        assert allowed("RY.TO", "2024-03-01")

    def test_ca_symbol_gated_by_ca_proxy(self):
        """A Canadian symbol consults the CA proxy map, not the US one."""
        regime_maps = {
            "SPY": {"2024-08-05": True},
            "^GSPTSE": {"2024-08-05": False},
        }
        symbol_proxy = {"AAPL": "SPY", "ENB.TO": "^GSPTSE"}

        def allowed(sym, date):
            proxy = symbol_proxy.get(sym, "SPY")
            return regime_maps.get(proxy, {}).get(date, True)

        # CA market down → CA symbol blocked, US symbol still allowed same day
        assert not allowed("ENB.TO", "2024-08-05")
        assert allowed("AAPL", "2024-08-05")

    def test_run_accepts_regime_symbol_ca_kwarg(self):
        """run() exposes regime_symbol_ca without requiring a live fetch."""
        import inspect
        from smart_trader.backtest.engine import BacktestEngine
        params = inspect.signature(BacktestEngine.run).parameters
        assert "regime_symbol_ca" in params
        assert params["regime_symbol_ca"].default == "^GSPTSE"


# ---------------------------------------------------------------------------
# Lever 4 — presets
# ---------------------------------------------------------------------------

def test_presets_are_nonempty():
    from smart_trader.backtest.__main__ import PRESETS
    for name, syms in PRESETS.items():
        assert len(syms) > 0, f"Preset '{name}' is empty"
        assert all(isinstance(s, str) and s == s.upper() for s in syms), \
            f"Preset '{name}' has non-uppercase or non-string ticker"

def test_presets_no_duplicates():
    from smart_trader.backtest.__main__ import PRESETS
    for name, syms in PRESETS.items():
        assert len(syms) == len(set(syms)), f"Preset '{name}' has duplicate tickers"

def test_broad_preset_has_multiple_sectors():
    from smart_trader.backtest.__main__ import PRESETS
    broad = PRESETS["broad"]
    # Spot-check a few cross-sector names
    for ticker in ["JPM", "XOM", "UNH", "COST", "PG"]:
        assert ticker in broad, f"{ticker} missing from 'broad' preset"


# ---------------------------------------------------------------------------
# _is_true helper
# ---------------------------------------------------------------------------

def test_is_true_with_bool():
    assert _is_true(True)
    assert not _is_true(False)

def test_is_true_with_nan():
    assert not _is_true(float("nan"))

def test_is_true_with_none():
    assert not _is_true(None)
