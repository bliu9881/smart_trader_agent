"""
Risk Manager — Absolute Veto Over All Trading Decisions.

This layer operates INDEPENDENTLY of the HMM. Even if the HMM fails completely,
circuit breakers catch drawdowns based on actual P&L. Defense in depth.

Circuit breakers (per spec):
  Daily DD > 2% → cut all sizes 50% rest of day
  Daily DD > 3% → close ALL, halt rest of day
  Weekly DD > 5% → cut sizes 50% rest of week
  Weekly DD > 7% → close ALL, halt rest of week
  Peak DD > 10% → halt ALL, write trading_halted.lock

Portfolio limits:
  Max total exposure: 80% (20% cash floor — exceeded when leverage > 1)
  Max single position: 15%
  Max sector exposure: 30%
  Max concurrent positions: 5
  Max daily trades: 20
  Max portfolio leverage: 1.25x

Position-level:
  Stop loss REQUIRED on every signal (reject if missing)
  Max 1% portfolio risk per trade
  Min position $100
  Gap risk: overnight sized so 3x stop-through ≤ 2% portfolio
  60-day rolling correlation: reduce 50% above 0.70, reject above 0.85
"""
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from smart_trader.core.signal import Signal
from smart_trader.settings.config import RiskConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    """Snapshot of portfolio state used by the risk manager."""
    equity: float
    cash: float
    buying_power: float
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    peak_equity: float = 0.0
    drawdown: float = 0.0
    circuit_breaker_status: str = "ok"
    flicker_rate: float = 0.0

    @property
    def total_exposure_pct(self) -> float:
        """Fraction of equity deployed in positions."""
        if self.equity <= 0:
            return 0.0
        total_value = sum(abs(p.get("market_value", 0)) for p in self.positions.values())
        return total_value / self.equity

    @property
    def sector_exposure_pct(self) -> Dict[str, float]:
        """Fraction of equity per sector."""
        if self.equity <= 0:
            return {}
        sector_values: Dict[str, float] = defaultdict(float)
        for symbol, pos in self.positions.items():
            sector = pos.get("sector", "unknown")
            sector_values[sector] += abs(pos.get("market_value", 0))
        return {s: v / self.equity for s, v in sector_values.items()}


@dataclass
class RiskDecision:
    """Risk manager's decision on a signal."""
    approved: bool
    modified_signal: Optional[Signal] = None
    rejection_reason: str = ""
    modifications: List[str] = field(default_factory=list)
    action: str = "none"  # none | half_size | close_all | halt

    @property
    def signal(self) -> Optional[Signal]:
        return self.modified_signal


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Tracks daily, weekly, and peak-drawdown circuit breakers."""

    def __init__(self, config: RiskConfig):
        self.config = config
        self.daily_start_equity = 0.0
        self.weekly_start_equity = 0.0
        self.peak_equity = 0.0
        self.current_equity = 0.0
        self.is_half_size = False
        self.weekly_half_size = False
        self.daily_closed = False
        self.weekly_closed = False
        self.is_halted = False
        self._last_daily_reset: Optional[date] = None
        self._last_weekly_reset: Optional[date] = None
        self._history: List[Dict] = []

    def initialize(self, equity: float) -> None:
        self.daily_start_equity = equity
        self.weekly_start_equity = equity
        self.peak_equity = equity
        self.current_equity = equity
        self.is_half_size = False
        self.weekly_half_size = False
        self.daily_closed = False
        self.weekly_closed = False
        today = date.today()
        self._last_daily_reset = today
        self._last_weekly_reset = today

        if Path(self.config.lock_file_path).exists():
            self.is_halted = True
            logger.critical(
                f"TRADING HALTED: lock file at {self.config.lock_file_path}. "
                "Delete manually to resume."
            )

    def update(self, equity: float) -> Tuple[bool, str, str]:
        """
        Update equity and check breakers.
        Returns (approved, action, reason).
        """
        today = date.today()
        self.current_equity = equity

        # Daily reset
        if self._last_daily_reset != today:
            self.daily_start_equity = equity
            self._last_daily_reset = today
            self.is_half_size = False
            self.daily_closed = False
            logger.info(f"Daily reset: start equity = ${equity:,.2f}")

        # Weekly reset on Monday
        if today.weekday() == 0 and self._last_weekly_reset != today:
            self.weekly_start_equity = equity
            self._last_weekly_reset = today
            self.weekly_half_size = False
            self.weekly_closed = False

        # Update peak
        if equity > self.peak_equity:
            self.peak_equity = equity

        if self.is_halted:
            return False, "halt", f"HALTED: lock file exists at {self.config.lock_file_path}"

        daily_pnl = (equity - self.daily_start_equity) / self.daily_start_equity if self.daily_start_equity else 0
        weekly_pnl = (equity - self.weekly_start_equity) / self.weekly_start_equity if self.weekly_start_equity else 0
        peak_dd = (equity - self.peak_equity) / self.peak_equity if self.peak_equity else 0

        # Most severe first
        if peak_dd <= -self.config.peak_drawdown_stop:
            self.is_halted = True
            self._write_lock_file(equity, peak_dd)
            self._record_trigger("peak_halt", peak_dd, equity)
            return False, "halt", f"Peak drawdown {peak_dd:.2%} exceeded {self.config.peak_drawdown_stop:.0%}"

        if weekly_pnl <= -self.config.weekly_loss_close_all:
            self.weekly_closed = True
            self._record_trigger("weekly_close_all", weekly_pnl, equity)
            return False, "close_all", f"Weekly loss {weekly_pnl:.2%} exceeded {self.config.weekly_loss_close_all:.0%}"

        if daily_pnl <= -self.config.daily_loss_close_all:
            self.daily_closed = True
            self._record_trigger("daily_close_all", daily_pnl, equity)
            return False, "close_all", f"Daily loss {daily_pnl:.2%} exceeded {self.config.daily_loss_close_all:.0%}"

        if weekly_pnl <= -self.config.weekly_loss_half_size:
            self.weekly_half_size = True
            self._record_trigger("weekly_half_size", weekly_pnl, equity)
            return True, "half_size", f"Weekly loss {weekly_pnl:.2%}: half size"

        if daily_pnl <= -self.config.daily_loss_half_size:
            self.is_half_size = True
            self._record_trigger("daily_half_size", daily_pnl, equity)
            return True, "half_size", f"Daily loss {daily_pnl:.2%}: half size"

        return True, "none", "all clear"

    def any_half_size(self) -> bool:
        return self.is_half_size or self.weekly_half_size

    def any_closed(self) -> bool:
        return self.daily_closed or self.weekly_closed

    def get_status(self) -> str:
        if self.is_halted:
            return "halted"
        if self.any_closed():
            return "closed"
        if self.any_half_size():
            return "half_size"
        return "ok"

    def _write_lock_file(self, equity: float, drawdown: float) -> None:
        lock_path = Path(self.config.lock_file_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "halted_at": datetime.now().isoformat(),
            "current_equity": equity,
            "peak_equity": self.peak_equity,
            "drawdown": f"{drawdown:.2%}",
            "action_required": "Review the cause, then delete this file to resume.",
        }
        lock_path.write_text(json.dumps(data, indent=2))
        logger.critical(f"Lock file written to {lock_path}")

    def _record_trigger(self, breaker: str, pnl: float, equity: float) -> None:
        self._history.append({
            "timestamp": datetime.now().isoformat(),
            "breaker": breaker,
            "pnl": pnl,
            "equity": equity,
        })

    def get_history(self) -> List[Dict]:
        return list(self._history)


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Validates and optionally modifies signals before they reach the broker."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.circuit_breaker = CircuitBreaker(self.config)
        # Returns history per symbol for 60-day correlation
        self._returns_history: Dict[str, deque] = {}
        self._trade_log: deque = deque(maxlen=1000)
        self._daily_trade_count = 0
        self._last_trade_date: Optional[date] = None

    def initialize(self, equity: float) -> None:
        self.circuit_breaker.initialize(equity)
        logger.info(f"Risk manager initialized with equity=${equity:,.2f}")

    def update_returns_history(self, symbol: str, returns: np.ndarray) -> None:
        """Feed historical returns for correlation calculations."""
        dq = deque(returns[-self.config.correlation_window:], maxlen=self.config.correlation_window)
        self._returns_history[symbol] = dq

    # --- the main entry point ---------------------------------------------

    def validate_signal(self, signal: Signal, portfolio_state: PortfolioState) -> RiskDecision:
        """
        Validate (and possibly modify) a signal.

        Order of checks (first failure wins):
          1. Mandatory stop loss
          2. Circuit breaker (portfolio-level)
          3. Max concurrent positions
          4. Max daily trades
          5. Duplicate order window
          6. Portfolio exposure limit (80%)
          7. Single-position limit (15%)
          8. Sector exposure limit (30%)
          9. Correlation check (60-day rolling)
         10. Leverage gating (force 1.0x in defensive conditions)
         11. Gap risk sizing
         12. Half-size adjustment (from circuit breaker)
         13. Minimum position size
        """
        modifications: List[str] = []

        # 1. Mandatory stop loss
        if signal.stop_loss is None or signal.stop_loss <= 0:
            return RiskDecision(approved=False, rejection_reason="Stop loss is mandatory; signal has none.")
        if signal.direction == "LONG" and signal.stop_loss >= signal.entry_price:
            return RiskDecision(approved=False, rejection_reason=f"LONG stop {signal.stop_loss:.2f} >= entry {signal.entry_price:.2f}")

        # 2. Circuit breaker
        approved, action, reason = self.circuit_breaker.update(portfolio_state.equity)
        if not approved:
            return RiskDecision(approved=False, rejection_reason=reason, action=action)

        # 3. Max concurrent positions
        is_new_position = signal.symbol not in portfolio_state.positions
        if is_new_position and len(portfolio_state.positions) >= self.config.max_concurrent_positions:
            return RiskDecision(
                approved=False,
                rejection_reason=f"Max concurrent positions ({self.config.max_concurrent_positions}) reached",
            )

        # 4. Max daily trades
        today = date.today()
        if self._last_trade_date != today:
            self._daily_trade_count = 0
            self._last_trade_date = today
        if self._daily_trade_count >= self.config.max_daily_trades:
            return RiskDecision(
                approved=False,
                rejection_reason=f"Max daily trades ({self.config.max_daily_trades}) reached",
            )

        # 5. Duplicate within window
        dup_cutoff = datetime.now() - timedelta(seconds=self.config.duplicate_order_window_sec)
        for entry in reversed(self._trade_log):
            if entry["time"] < dup_cutoff:
                break
            if entry["symbol"] == signal.symbol and entry["direction"] == signal.direction:
                return RiskDecision(
                    approved=False,
                    rejection_reason=f"Duplicate {signal.direction} for {signal.symbol} within {self.config.duplicate_order_window_sec}s",
                )

        # 6-8. Compute working size and check hard caps
        size_pct = signal.position_size_pct

        # If the signal has no explicit size (0.0 = "auto-size"), compute one
        # from max_risk_per_trade using the stop-loss distance.
        if size_pct <= 0 and signal.stop_loss and signal.stop_loss > 0:
            risk_per_share = abs(signal.entry_price - signal.stop_loss)
            if risk_per_share > 0 and portfolio_state.equity > 0:
                max_risk = portfolio_state.equity * self.config.max_risk_per_trade
                shares = max_risk / risk_per_share
                size_pct = (shares * signal.entry_price) / portfolio_state.equity

        # 7. Single-position cap
        if size_pct > self.config.max_single_position:
            modifications.append(
                f"Clipped position from {size_pct:.0%} to max_single {self.config.max_single_position:.0%}"
            )
            size_pct = self.config.max_single_position

        # 6. Total exposure cap
        existing_exposure = portfolio_state.total_exposure_pct
        current_symbol_exposure = 0.0
        if not is_new_position:
            current_symbol_exposure = abs(
                portfolio_state.positions[signal.symbol].get("market_value", 0)
            ) / max(portfolio_state.equity, 1.0)
        other_exposure = existing_exposure - current_symbol_exposure
        room = max(self.config.max_total_exposure - other_exposure, 0.0)
        if size_pct > room:
            if room <= 0:
                return RiskDecision(
                    approved=False,
                    rejection_reason=f"Total exposure {existing_exposure:.0%} already at cap {self.config.max_total_exposure:.0%}",
                )
            modifications.append(
                f"Clipped from {size_pct:.0%} to {room:.0%} (total exposure cap)"
            )
            size_pct = room

        # 8. Sector cap
        sector = self.config.sector_map.get(signal.symbol, "unknown")
        sector_expo = portfolio_state.sector_exposure_pct.get(sector, 0.0)
        # Subtract this symbol's current contribution to the sector
        sector_room = max(self.config.max_sector_exposure - sector_expo + current_symbol_exposure, 0.0)
        if size_pct > sector_room:
            if sector_room <= 0:
                return RiskDecision(
                    approved=False,
                    rejection_reason=f"Sector '{sector}' at cap {self.config.max_sector_exposure:.0%}",
                )
            modifications.append(
                f"Clipped from {size_pct:.0%} to {sector_room:.0%} (sector '{sector}' cap)"
            )
            size_pct = sector_room

        # 9. Correlation check
        corr_decision = self._check_correlation(signal.symbol, portfolio_state)
        if corr_decision is not None:
            if corr_decision[0] == "reject":
                return RiskDecision(approved=False, rejection_reason=corr_decision[1])
            if corr_decision[0] == "reduce":
                size_pct *= 0.5
                modifications.append(corr_decision[1])

        # 10. Leverage gating
        leverage = signal.leverage
        force_1x = (
            self.circuit_breaker.any_half_size()
            or len(portfolio_state.positions) >= 3
            or portfolio_state.flicker_rate > 0.2
        )
        if force_1x and leverage > 1.0:
            modifications.append(f"Forced leverage 1.0x (was {leverage:.2f}x)")
            leverage = 1.0
        if leverage > self.config.max_leverage:
            modifications.append(f"Clipped leverage {leverage:.2f}x → {self.config.max_leverage:.2f}x")
            leverage = self.config.max_leverage

        # 11. Gap risk sizing (uses stop-loss distance)
        risk_per_share = abs(signal.entry_price - signal.stop_loss)
        if risk_per_share > 0:
            max_gap_loss = portfolio_state.equity * self.config.gap_risk_max_loss
            max_shares_gap = max_gap_loss / (risk_per_share * self.config.gap_risk_multiplier)
            max_value_gap = max_shares_gap * signal.entry_price
            max_size_pct_gap = max_value_gap / max(portfolio_state.equity, 1.0)
            if size_pct > max_size_pct_gap and max_size_pct_gap > 0:
                modifications.append(
                    f"Gap risk: clipped {size_pct:.0%} → {max_size_pct_gap:.0%} "
                    f"(3x stop-through cap)"
                )
                size_pct = max_size_pct_gap

        # 12. Half-size adjustment
        if self.circuit_breaker.any_half_size():
            original = size_pct
            size_pct *= 0.5
            modifications.append(f"Half-size mode: {original:.0%} → {size_pct:.0%}")

        # 13. Minimum position
        position_value = size_pct * portfolio_state.equity
        if position_value < self.config.min_position_size:
            return RiskDecision(
                approved=False,
                rejection_reason=f"Position ${position_value:,.0f} below min ${self.config.min_position_size:,.0f}",
            )

        # Build modified signal
        from dataclasses import replace
        modified = replace(signal, position_size_pct=size_pct, leverage=leverage)

        # Record the trade
        self._trade_log.append({
            "time": datetime.now(),
            "symbol": signal.symbol,
            "direction": signal.direction,
        })
        self._daily_trade_count += 1

        return RiskDecision(
            approved=True,
            modified_signal=modified,
            modifications=modifications,
            action=action,
        )

    # --- signal-driven exit validation ------------------------------------

    def validate_exit_signal(self, signal: Signal) -> RiskDecision:
        """Lightweight gate for signal-driven exits (direction == "FLAT").

        Exits skip the entry-only checks (mandatory stop loss, sizing,
        sector/correlation/concurrency caps) — those constrain *opening*
        positions. The remaining hazards we still police on the way out:

          * Halt lock: respect the intentional-friction halt. The lock file
            exists because a human is supposed to review before more orders
            fire. If an operator wants to flatten while halted they can do
            so manually — see the rationale in the design plan.
          * Duplicate-order window: prevents a fast double-close on the
            same symbol if the same exit signal recurs across consecutive
            cycles.
          * Daily-trade cap: exits count toward the same budget as entries.

        Half-size and close-all states do NOT block exits (a close-all
        circuit breaker WANTS positions closed; signal-driven exits during
        close-all are merely redundant, not harmful).
        """
        if signal.direction != "FLAT":
            return RiskDecision(
                approved=False,
                rejection_reason=f"validate_exit_signal expects FLAT, got {signal.direction}",
            )

        if self.circuit_breaker.is_halted:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"HALTED: lock file at {self.config.lock_file_path}. "
                    "Manual review required before any orders, including exits."
                ),
                action="halt",
            )

        today = date.today()
        if self._last_trade_date != today:
            self._daily_trade_count = 0
            self._last_trade_date = today
        if self._daily_trade_count >= self.config.max_daily_trades:
            return RiskDecision(
                approved=False,
                rejection_reason=(
                    f"Max daily trades ({self.config.max_daily_trades}) reached"
                ),
            )

        dup_cutoff = datetime.now() - timedelta(seconds=self.config.duplicate_order_window_sec)
        for entry in reversed(self._trade_log):
            if entry["time"] < dup_cutoff:
                break
            if entry["symbol"] == signal.symbol and entry["direction"] == signal.direction:
                return RiskDecision(
                    approved=False,
                    rejection_reason=(
                        f"Duplicate {signal.direction} for {signal.symbol} "
                        f"within {self.config.duplicate_order_window_sec}s"
                    ),
                )

        self._trade_log.append({
            "time": datetime.now(),
            "symbol": signal.symbol,
            "direction": signal.direction,
        })
        self._daily_trade_count += 1

        return RiskDecision(approved=True, modified_signal=signal)

    # --- helpers ----------------------------------------------------------

    def _check_correlation(self, symbol: str, portfolio_state: PortfolioState) -> Optional[Tuple[str, str]]:
        """
        Return None (OK), ("reduce", reason), or ("reject", reason).
        Uses 60-day rolling correlation of returns against existing positions.
        """
        new_ret = self._returns_history.get(symbol)
        if new_ret is None or len(new_ret) < 20:
            return None

        new_arr = np.asarray(new_ret)
        for existing_symbol in portfolio_state.positions:
            if existing_symbol == symbol:
                continue
            existing_ret = self._returns_history.get(existing_symbol)
            if existing_ret is None or len(existing_ret) < 20:
                continue
            existing_arr = np.asarray(existing_ret)
            n = min(len(new_arr), len(existing_arr))
            if n < 20:
                continue
            corr = np.corrcoef(new_arr[-n:], existing_arr[-n:])[0, 1]
            if np.isnan(corr):
                continue
            if abs(corr) > self.config.correlation_reject_threshold:
                return ("reject", f"Correlation with {existing_symbol} = {corr:.2f} > {self.config.correlation_reject_threshold:.2f}")
            if abs(corr) > self.config.correlation_reduce_threshold:
                return ("reduce", f"Correlation with {existing_symbol} = {corr:.2f} > {self.config.correlation_reduce_threshold:.2f}, size halved")
        return None

    def compute_position_size(
        self,
        price: float,
        stop_loss: float,
        portfolio_value: float,
        risk_pct_override: Optional[float] = None,
    ) -> int:
        """
        Compute size such that loss if stopped = risk_pct × portfolio.

        `risk_pct_override` lets high-conviction paths (e.g. the overlap
        signal where Path A and Path B agree) use a larger per-trade risk
        than the default `max_risk_per_trade`. Half-size mode and
        min_position_size clipping still apply on top.
        """
        if stop_loss >= price:
            return 0
        risk_per_share = price - stop_loss
        risk_pct = (
            risk_pct_override
            if risk_pct_override is not None
            else self.config.max_risk_per_trade
        )
        max_risk = portfolio_value * risk_pct
        shares = int(max_risk / risk_per_share)
        if self.circuit_breaker.any_half_size():
            shares = max(1, shares // 2)
        if shares * price < self.config.min_position_size:
            return 0
        return shares

    def get_risk_status(self) -> Dict:
        cb = self.circuit_breaker
        daily_pnl = ((cb.current_equity - cb.daily_start_equity) / cb.daily_start_equity) if cb.daily_start_equity else 0
        weekly_pnl = ((cb.current_equity - cb.weekly_start_equity) / cb.weekly_start_equity) if cb.weekly_start_equity else 0
        peak_dd = ((cb.current_equity - cb.peak_equity) / cb.peak_equity) if cb.peak_equity else 0
        return {
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "peak_drawdown": peak_dd,
            "status": cb.get_status(),
            "is_half_size": cb.any_half_size(),
            "is_closed": cb.any_closed(),
            "is_halted": cb.is_halted,
            "peak_equity": cb.peak_equity,
            "current_equity": cb.current_equity,
            "circuit_breakers": {
                "daily_half_size": f"{self.config.daily_loss_half_size:.0%}",
                "daily_close_all": f"{self.config.daily_loss_close_all:.0%}",
                "weekly_half_size": f"{self.config.weekly_loss_half_size:.0%}",
                "weekly_close_all": f"{self.config.weekly_loss_close_all:.0%}",
                "peak_drawdown_stop": f"{self.config.peak_drawdown_stop:.0%}",
            },
        }
