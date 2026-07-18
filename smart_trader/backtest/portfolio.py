"""Simulated portfolio for backtesting — no IBKR dependency.

Tracks cash, open positions, and closed trades. Position sizing follows the
same 1%-risk-per-trade formula used by the live RiskManager.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from smart_trader.settings.config import AppConfig


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    shares: int
    stop_loss: float
    take_profit: float
    trail_pct: float
    signal_type: str        # "gap_up" | "ema_pullback"
    confidence: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "stop" | "take_profit" | "trail" | "end_of_test"
    pnl: Optional[float] = None         # net of slippage + commissions
    entry_commission: float = 0.0
    exit_commission: float = 0.0


@dataclass
class _OpenPosition:
    trade: Trade
    trail_level: float


class SimulatedPortfolio:
    """Cash + open-position tracker for backtest replay."""

    def __init__(
        self,
        initial_equity: float,
        config: AppConfig,
        slippage_bps: float = 0.0,
        commission_per_share: float = 0.0,
        min_commission: float = 0.0,
    ):
        """Cash + open-position tracker for backtest replay.

        Transaction-cost model (defaults to ZERO so direct unit tests stay
        frictionless; BacktestEngine.run() supplies realistic defaults):
          - slippage_bps: adverse price slip in basis points, applied to the
            entry and to MARKET-type exits (hard stop, trailing stop). A
            take-profit is a resting limit, so it gets no adverse slip.
          - commission_per_share / min_commission: per-order IBKR-style
            commission (capped at 1% of notional), charged on entry and exit.
        """
        self._cash = initial_equity
        self._cfg = config
        self._slip = max(0.0, slippage_bps) / 10_000.0
        self._comm_per_share = max(0.0, commission_per_share)
        self._min_comm = max(0.0, min_commission)
        self._open: Dict[str, _OpenPosition] = {}
        self._closed: List[Trade] = []
        self._equity_history: List[float] = []

    def _commission(self, shares: int, price: float) -> float:
        """IBKR-style per-order commission: per-share with a floor, capped at
        1% of notional (so the floor never dominates a tiny order)."""
        if shares <= 0 or (self._comm_per_share <= 0 and self._min_comm <= 0):
            return 0.0
        raw = max(self._comm_per_share * shares, self._min_comm)
        return min(raw, 0.01 * shares * price)

    # ------------------------------------------------------------------ reads

    @property
    def open_symbols(self) -> set:
        return set(self._open)

    def open_count(self) -> int:
        return len(self._open)

    def equity(self, current_prices: Optional[Dict[str, float]] = None) -> float:
        """Mark-to-market equity: cash + open positions at current_prices."""
        position_value = 0.0
        for sym, pos in self._open.items():
            price = (current_prices or {}).get(sym, pos.trade.entry_price)
            position_value += price * pos.trade.shares
        return self._cash + position_value

    def trade_log(self) -> List[Trade]:
        return list(self._closed)

    def equity_history(self) -> List[float]:
        return list(self._equity_history)

    # ----------------------------------------------------------------- writes

    def open_position(
        self,
        symbol: str,
        date: str,
        price: float,
        signal_type: str,
        confidence: float,
    ) -> bool:
        """Attempt to open a new long position. Returns True if opened."""
        if len(self._open) >= self._cfg.risk.max_concurrent_positions:
            return False
        if symbol in self._open:
            return False
        if price <= 0:
            return False

        stop_pct = self._cfg.trader.default_stop_pct
        trail_pct = self._cfg.trader.default_trail_pct
        take_profit_pct = self._cfg.trader.default_take_profit_pct
        risk_per_trade = self._cfg.risk.max_risk_per_trade
        max_single_pct = self._cfg.risk.max_single_position

        eq = self.equity()
        # Stop / take-profit levels are set off the nominal signal price (as the
        # live system does); slippage affects the realized fill, not the levels.
        stop_loss = price * (1.0 - stop_pct)
        take_profit = price * (1.0 + take_profit_pct)
        stop_distance = price - stop_loss
        fill_price = price * (1.0 + self._slip)  # adverse slip on the buy

        # Risk-based sizing (off nominal price/stop): lose ~risk_per_trade on a stop-out
        shares = math.floor((eq * risk_per_trade) / stop_distance)
        # Cap at max single position
        shares = min(shares, math.floor((eq * max_single_pct) / price))

        if shares <= 0:
            return False

        commission = self._commission(shares, fill_price)
        cost = shares * fill_price + commission
        if cost > self._cash:
            # Scale to fit cash incl. commission (≤1% of notional → 1.01 buffer)
            shares = math.floor(self._cash / (fill_price * 1.01))
            if shares <= 0:
                return False
            commission = self._commission(shares, fill_price)
            cost = shares * fill_price + commission

        self._cash -= cost
        trade = Trade(
            symbol=symbol,
            entry_date=date,
            entry_price=fill_price,
            shares=shares,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trail_pct=trail_pct,
            signal_type=signal_type,
            confidence=confidence,
            entry_commission=commission,
        )
        self._open[symbol] = _OpenPosition(trade=trade, trail_level=price * (1.0 - trail_pct))
        return True

    def update_exits(
        self,
        date: str,
        day_bars: Dict[str, pd.Series],
    ) -> List[Trade]:
        """Check all open positions against today's high/low/close.

        Exit priority (from most to least): stop loss > take profit > trail.
        When both stop and take-profit trigger (rare), stop wins (conservative).
        Returns the trades closed today.
        """
        closed_today: List[Trade] = []
        to_close: List[str] = []

        for sym, pos in self._open.items():
            bars = day_bars.get(sym)
            if bars is None:
                continue

            low = float(bars.get("low", pos.trade.entry_price))
            high = float(bars.get("high", pos.trade.entry_price))
            close = float(bars.get("close", pos.trade.entry_price))

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            if low <= pos.trade.stop_loss:
                exit_price = pos.trade.stop_loss
                exit_reason = "stop"
            elif high >= pos.trade.take_profit:
                exit_price = pos.trade.take_profit
                exit_reason = "take_profit"
            else:
                # Update trailing stop then check
                new_trail = close * (1.0 - pos.trade.trail_pct)
                pos.trail_level = max(pos.trail_level, new_trail)
                if low <= pos.trail_level:
                    exit_price = pos.trail_level
                    exit_reason = "trail"

            if exit_price is not None:
                # Hard stop and trailing stop fill at market → adverse slip.
                # Take-profit is a resting limit → no adverse slip.
                if exit_reason in ("stop", "trail"):
                    exit_price *= (1.0 - self._slip)
                t = pos.trade
                commission = self._commission(t.shares, exit_price)
                t.exit_date = date
                t.exit_price = exit_price
                t.exit_reason = exit_reason
                t.exit_commission = commission
                t.pnl = (exit_price - t.entry_price) * t.shares - t.entry_commission - commission
                self._cash += exit_price * t.shares - commission
                closed_today.append(t)
                to_close.append(sym)

        for sym in to_close:
            self._closed.append(self._open.pop(sym).trade)

        return closed_today

    def close_all(self, date: str, prices: Dict[str, float]) -> List[Trade]:
        """Close all remaining positions at end-of-test using given prices."""
        closed: List[Trade] = []
        for sym in list(self._open):
            pos = self._open[sym]
            price = prices.get(sym, pos.trade.entry_price)
            t = pos.trade
            # End-of-test liquidation is a market exit → adverse slip + commission.
            fill = price * (1.0 - self._slip)
            commission = self._commission(t.shares, fill)
            t.exit_date = date
            t.exit_price = fill
            t.exit_reason = "end_of_test"
            t.exit_commission = commission
            t.pnl = (fill - t.entry_price) * t.shares - t.entry_commission - commission
            self._cash += fill * t.shares - commission
            self._closed.append(t)
            closed.append(t)
        self._open.clear()
        return closed

    def record_equity(self, current_prices: Optional[Dict[str, float]] = None) -> float:
        """Snapshot today's equity and append to history."""
        eq = self.equity(current_prices)
        self._equity_history.append(eq)
        return eq
