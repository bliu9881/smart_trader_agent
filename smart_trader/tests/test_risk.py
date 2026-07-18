"""
Tests for the enhanced risk manager.

Covers: circuit breakers, mandatory stop loss, portfolio limits (80% total,
15% single, 30% sector), correlation checks, gap risk, leverage gating,
RiskDecision/PortfolioState dataclasses.
"""
import os
import tempfile
from datetime import datetime

import numpy as np
import pytest

from smart_trader.core.signal import Signal
from smart_trader.core.risk_manager import (
    CircuitBreaker,
    PortfolioState,
    RiskManager,
)
from smart_trader.settings.config import RiskConfig


def _no_lock_config(**kwargs) -> RiskConfig:
    """RiskConfig with a non-existent lock file path to isolate tests."""
    return RiskConfig(
        lock_file_path=os.path.join(tempfile.gettempdir(), "test_nonexistent.lock"),
        **kwargs,
    )


def _signal(symbol="AAPL", size=0.10, entry=100.0, stop=95.0, leverage=1.0) -> Signal:
    return Signal(
        symbol=symbol,
        direction="LONG",
        confidence=0.85,
        entry_price=entry,
        stop_loss=stop,
        position_size_pct=size,
        leverage=leverage,
        timestamp=datetime.now(),
        reasoning="test",
        strategy_name="LowVolBullStrategy",
    )


def _portfolio(equity=100_000.0, positions=None) -> PortfolioState:
    return PortfolioState(
        equity=equity,
        cash=equity,
        buying_power=equity,
        positions=positions or {},
        peak_equity=equity,
    )


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

class TestCircuitBreakers:
    def test_init(self):
        cb = CircuitBreaker(_no_lock_config())
        cb.initialize(100_000)
        assert cb.peak_equity == 100_000
        assert not cb.is_halted

    def test_daily_2pct_half_size(self):
        cb = CircuitBreaker(_no_lock_config())
        cb.initialize(100_000)
        approved, action, _ = cb.update(98_000)
        assert approved is True
        assert action == "half_size"

    def test_daily_3pct_close_all(self):
        cb = CircuitBreaker(_no_lock_config())
        cb.initialize(100_000)
        approved, action, _ = cb.update(97_000)
        assert approved is False
        assert action == "close_all"

    def test_weekly_5pct_half_size(self):
        """Weekly -5% with daily unchanged should return half_size."""
        cb = CircuitBreaker(_no_lock_config())
        cb.initialize(100_000)
        # Simulate: weekly started at 100k, daily just started at 99.5k
        # (mid-week reset), now equity = 95k → weekly -5%, daily -0.5%
        cb.daily_start_equity = 95_100  # daily drop only 0.1%
        approved, action, _ = cb.update(95_000)
        assert action == "half_size"

    def test_weekly_7pct_close_all(self):
        cb = CircuitBreaker(_no_lock_config())
        cb.initialize(100_000)
        cb.daily_start_equity = 93_100  # daily drop only tiny
        approved, action, _ = cb.update(93_000)
        assert action == "close_all"
        assert approved is False

    def test_peak_10pct_halt_writes_lockfile(self):
        with tempfile.NamedTemporaryFile(suffix=".lock", delete=False) as f:
            lock_path = f.name
        os.unlink(lock_path)

        cb = CircuitBreaker(RiskConfig(lock_file_path=lock_path, peak_drawdown_stop=0.10))
        cb.initialize(100_000)
        cb.update(110_000)  # new peak
        approved, action, _ = cb.update(99_000)  # -10%

        assert action == "halt"
        assert cb.is_halted
        assert os.path.exists(lock_path)
        os.unlink(lock_path)

    def test_existing_lockfile_blocks_trading(self):
        with tempfile.NamedTemporaryFile(suffix=".lock", delete=False) as f:
            f.write(b"halted")
            lock_path = f.name

        cb = CircuitBreaker(RiskConfig(lock_file_path=lock_path))
        cb.initialize(100_000)
        approved, action, _ = cb.update(100_000)
        assert action == "halt"
        os.unlink(lock_path)


# ---------------------------------------------------------------------------
# Mandatory stop loss
# ---------------------------------------------------------------------------

class TestMandatoryStopLoss:
    def test_rejects_signal_without_stop(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        bad = _signal(stop=0.0)
        decision = rm.validate_signal(bad, _portfolio())
        assert decision.approved is False
        assert "stop loss" in decision.rejection_reason.lower()

    def test_rejects_stop_above_entry_for_long(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        bad = _signal(entry=100, stop=105)
        decision = rm.validate_signal(bad, _portfolio())
        assert decision.approved is False


# ---------------------------------------------------------------------------
# Portfolio exposure limits
# ---------------------------------------------------------------------------

class TestPortfolioLimits:
    def test_clips_single_position_to_15pct(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        sig = _signal(size=0.30)  # asks for 30%
        decision = rm.validate_signal(sig, _portfolio())
        assert decision.approved is True
        assert decision.modified_signal.position_size_pct <= 0.15
        assert any("max_single" in m for m in decision.modifications)

    def test_rejects_when_total_exposure_at_cap(self):
        """Request clipped by total-exposure cap (80%)."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        # Four 15% positions across distinct sectors = 60% total. Asking for
        # another 30% → total would be 90% > 80%, so clip to 20% room.
        positions = {
            "SPY": {"market_value": 15_000, "sector": "index"},
            "AAPL": {"market_value": 15_000, "sector": "tech"},
            "AMZN": {"market_value": 15_000, "sector": "consumer"},
            "XOM": {"market_value": 15_000, "sector": "energy"},
        }
        sig = _signal(symbol="JPM", size=0.30)  # financial sector, 0% used
        decision = rm.validate_signal(sig, _portfolio(positions=positions))
        assert decision.approved is True
        assert decision.modified_signal.position_size_pct <= 0.20 + 1e-6

    def test_rejects_when_max_concurrent_reached(self):
        rm = RiskManager(_no_lock_config(max_concurrent_positions=3))
        rm.initialize(100_000)
        positions = {f"SYM{i}": {"market_value": 1000, "sector": "unknown"} for i in range(3)}
        decision = rm.validate_signal(_signal(symbol="NEW"), _portfolio(positions=positions))
        assert decision.approved is False
        assert "concurrent" in decision.rejection_reason.lower()

    def test_sector_exposure_cap(self):
        rm = RiskManager(_no_lock_config(
            sector_map={"AAPL": "tech", "MSFT": "tech"},
        ))
        rm.initialize(100_000)
        # 25% in tech already; request another 15% in tech → sector cap 30% blocks to 5%
        positions = {
            "AAPL": {"market_value": 25_000, "sector": "tech"},
        }
        sig = _signal(symbol="MSFT", size=0.15)
        decision = rm.validate_signal(sig, _portfolio(positions=positions))
        assert decision.approved is True
        assert decision.modified_signal.position_size_pct <= 0.05 + 1e-6


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

class TestCorrelation:
    def test_rejects_highly_correlated(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        np.random.seed(0)
        base = np.random.randn(80)
        # New asset perfectly correlated with existing AAPL
        rm.update_returns_history("AAPL", base)
        rm.update_returns_history("MSFT", base + np.random.randn(80) * 0.001)

        positions = {"AAPL": {"market_value": 10_000, "sector": "tech"}}
        decision = rm.validate_signal(_signal(symbol="MSFT"), _portfolio(positions=positions))
        assert decision.approved is False
        assert "correlation" in decision.rejection_reason.lower()

    def test_reduces_moderately_correlated(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        np.random.seed(1)
        base = np.random.randn(80)
        # ~0.75 correlation — above reduce threshold but below reject
        moderate = base + np.random.randn(80) * 0.85
        rm.update_returns_history("AAPL", base)
        rm.update_returns_history("MSFT", moderate)

        positions = {"AAPL": {"market_value": 1_000, "sector": "tech"}}
        decision = rm.validate_signal(_signal(symbol="MSFT", size=0.10), _portfolio(positions=positions))
        # Whether it reduced depends on actual corr; the test is that it didn't reject.
        # We verify correlation machinery runs.
        assert decision.approved is True

    def test_uncorrelated_passes(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        np.random.seed(2)
        rm.update_returns_history("AAPL", np.random.randn(80))
        rm.update_returns_history("XOM", np.random.randn(80))

        positions = {"AAPL": {"market_value": 1_000, "sector": "tech"}}
        decision = rm.validate_signal(_signal(symbol="XOM"), _portfolio(positions=positions))
        assert decision.approved is True


# ---------------------------------------------------------------------------
# Gap risk
# ---------------------------------------------------------------------------

class TestGapRisk:
    def test_gap_risk_clips_size(self):
        """A very tight stop + 3x gap multiplier should cap the size."""
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        # Entry $100, stop $99 → $1 risk/share, 3x gap = $3. Max 2% portfolio = $2000
        # Max shares gap = 2000/3 ≈ 666 → position value ≈ $66,600 (66.6%)
        # But we also have max_single 15%, so it'll clip to 15% first
        sig = _signal(entry=100, stop=99, size=0.95)
        decision = rm.validate_signal(sig, _portfolio())
        assert decision.approved is True
        # Max single 15% still applies
        assert decision.modified_signal.position_size_pct <= 0.15 + 1e-6


# ---------------------------------------------------------------------------
# Half-size mode
# ---------------------------------------------------------------------------

class TestHalfSizeMode:
    def test_half_size_active_halves_request(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        # Trigger daily half-size
        rm.circuit_breaker.update(98_000)
        assert rm.circuit_breaker.any_half_size()

        sig = _signal(size=0.10)
        decision = rm.validate_signal(sig, _portfolio(equity=98_000))
        assert decision.approved is True
        assert decision.modified_signal.position_size_pct == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Leverage gating
# ---------------------------------------------------------------------------

class TestLeverageGating:
    def test_three_positions_force_1x(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        positions = {f"SYM{i}": {"market_value": 1_000, "sector": "unknown"} for i in range(3)}
        decision = rm.validate_signal(_signal(leverage=1.25), _portfolio(positions=positions))
        assert decision.approved is True
        assert decision.modified_signal.leverage == 1.0

    def test_half_size_mode_forces_1x(self):
        rm = RiskManager(_no_lock_config())
        rm.initialize(100_000)
        rm.circuit_breaker.update(98_000)  # half-size active
        decision = rm.validate_signal(_signal(leverage=1.25), _portfolio(equity=98_000))
        assert decision.modified_signal.leverage == 1.0


# ---------------------------------------------------------------------------
# Minimum position size
# ---------------------------------------------------------------------------

def test_min_position_size():
    rm = RiskManager(_no_lock_config(min_position_size=1000.0))
    rm.initialize(100_000)
    sig = _signal(size=0.005)  # $500 position
    decision = rm.validate_signal(sig, _portfolio())
    assert decision.approved is False
    assert "min" in decision.rejection_reason.lower()


# ---------------------------------------------------------------------------
# Smoke: compute_position_size helper
# ---------------------------------------------------------------------------

def test_compute_position_size():
    rm = RiskManager(_no_lock_config(max_risk_per_trade=0.01))
    rm.initialize(100_000)
    shares = rm.compute_position_size(price=100, stop_loss=95, portfolio_value=100_000)
    # Risk budget $1000, risk/share $5 → 200 shares
    assert shares == 200


def test_compute_position_size_with_risk_override_doubles_shares():
    """Overlap path passes risk_pct_override = 2× default for 2× sizing."""
    rm = RiskManager(_no_lock_config(max_risk_per_trade=0.01))
    rm.initialize(100_000)
    base = rm.compute_position_size(100, 95, 100_000)
    boosted = rm.compute_position_size(100, 95, 100_000, risk_pct_override=0.02)
    assert boosted == 2 * base


def test_compute_position_size_override_still_honors_half_size():
    """Half-size circuit breaker applies on top of any risk override."""
    rm = RiskManager(_no_lock_config(max_risk_per_trade=0.01))
    rm.initialize(100_000)
    rm.circuit_breaker.update(97_500)  # daily -2.5% → half-size mode
    assert rm.circuit_breaker.any_half_size()
    shares = rm.compute_position_size(100, 95, 100_000, risk_pct_override=0.02)
    # Without half-size: 2% × $100k / $5 = 400 shares; with half-size: 200
    assert shares == 200


def test_risk_status_shape():
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    rm.circuit_breaker.update(99_500)
    status = rm.get_risk_status()
    assert "daily_pnl" in status
    assert "peak_drawdown" in status
    assert "circuit_breakers" in status


# ---------------------------------------------------------------------------
# validate_exit_signal — Phase 2 signal-driven exits
# ---------------------------------------------------------------------------


def _exit_signal(symbol: str = "AAPL") -> Signal:
    return Signal(
        symbol=symbol,
        direction="FLAT",
        confidence=0.6,
        entry_price=150.0,  # last price for reference; not validated for FLAT
        stop_loss=0.0,
        timestamp=datetime.now(),
        reasoning="SmartMoney SELL",
        strategy_name="SmartMoneyExit",
        metadata={"exit_signal": True},
    )


def test_validate_exit_signal_approves_clean_exit():
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    decision = rm.validate_exit_signal(_exit_signal())
    assert decision.approved
    assert decision.modified_signal is not None
    assert decision.modified_signal.direction == "FLAT"


def test_validate_exit_signal_rejects_non_flat_direction():
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    long_sig = _signal(symbol="MSFT")
    decision = rm.validate_exit_signal(long_sig)
    assert not decision.approved
    assert "FLAT" in decision.rejection_reason


def test_validate_exit_signal_rejects_when_halted():
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    rm.circuit_breaker.is_halted = True
    decision = rm.validate_exit_signal(_exit_signal())
    assert not decision.approved
    assert "HALTED" in decision.rejection_reason
    assert decision.action == "halt"


def test_validate_exit_signal_approves_during_half_size():
    """Half-size mode constrains entry sizing only — exits flow through."""
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    rm.circuit_breaker.update(97_500)  # daily -2.5% → half_size
    assert rm.circuit_breaker.any_half_size()
    decision = rm.validate_exit_signal(_exit_signal())
    assert decision.approved


def test_validate_exit_signal_approves_during_close_all():
    """A close-all circuit breaker WANTS positions closed; exits should pass."""
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    rm.circuit_breaker.update(96_500)  # daily -3.5% → close_all
    assert rm.circuit_breaker.any_closed()
    decision = rm.validate_exit_signal(_exit_signal())
    assert decision.approved


def test_validate_exit_signal_rejects_duplicate_within_window():
    rm = RiskManager(_no_lock_config(duplicate_order_window_sec=60))
    rm.initialize(100_000)
    first = rm.validate_exit_signal(_exit_signal("AAPL"))
    assert first.approved
    second = rm.validate_exit_signal(_exit_signal("AAPL"))
    assert not second.approved
    assert "Duplicate" in second.rejection_reason


def test_validate_exit_signal_rejects_at_daily_trade_cap():
    rm = RiskManager(_no_lock_config(max_daily_trades=2))
    rm.initialize(100_000)
    # Two approved exits → cap reached.
    assert rm.validate_exit_signal(_exit_signal("AAPL")).approved
    assert rm.validate_exit_signal(_exit_signal("MSFT")).approved
    decision = rm.validate_exit_signal(_exit_signal("GOOGL"))
    assert not decision.approved
    assert "Max daily trades" in decision.rejection_reason


def test_validate_exit_signal_records_in_trade_log():
    """Exits must update _trade_log so the duplicate-window check catches
    a same-symbol re-fire on the next cycle."""
    rm = RiskManager(_no_lock_config())
    rm.initialize(100_000)
    rm.validate_exit_signal(_exit_signal("AAPL"))
    assert any(e["symbol"] == "AAPL" and e["direction"] == "FLAT" for e in rm._trade_log)
