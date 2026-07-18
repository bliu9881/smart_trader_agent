"""Backtest result metrics.

All inputs are post-simulation; no live data dependencies.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List

from smart_trader.backtest.portfolio import Trade


def compute_metrics(trades: List[Trade], equity_curve: List[float]) -> Dict[str, Any]:
    """Return summary metrics for a completed backtest.

    Returns {"error": ...} if there is insufficient data to compute metrics.
    """
    closed = [t for t in trades if t.pnl is not None and t.exit_reason != "end_of_test"]
    if not closed:
        return {"error": "no closed trades (excluding end-of-test liquidations)"}

    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_wins = sum(wins) if wins else 0.0
    gross_losses = abs(sum(losses)) if losses else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    total_pnl = sum(pnls)

    # Equity curve stats
    if len(equity_curve) < 2:
        return {"error": "equity curve too short"}

    initial = equity_curve[0]
    final = equity_curve[-1]
    total_return = (final - initial) / initial if initial else 0.0

    n_days = len(equity_curve)
    annualized_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0 if n_days > 0 else 0.0

    # Max drawdown
    peak = equity_curve[0]
    max_drawdown = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak if peak else 0.0
        if dd < max_drawdown:
            max_drawdown = dd

    # Daily Sharpe (risk-free = 0)
    sharpe = 0.0
    if n_days > 1:
        daily_rets = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, n_days)
            if equity_curve[i - 1] != 0
        ]
        if daily_rets:
            mean_r = sum(daily_rets) / len(daily_rets)
            variance = sum((r - mean_r) ** 2 for r in daily_rets) / len(daily_rets)
            std_r = math.sqrt(variance)
            if std_r > 0:
                sharpe = (mean_r / std_r) * math.sqrt(252)

    # Average holding period
    holding_days: List[int] = []
    for t in closed:
        if t.exit_date and t.entry_date:
            try:
                ed = datetime.strptime(t.exit_date, "%Y-%m-%d")
                en = datetime.strptime(t.entry_date, "%Y-%m-%d")
                holding_days.append((ed - en).days)
            except ValueError:
                pass
    avg_hold_days = sum(holding_days) / len(holding_days) if holding_days else 0.0

    # By signal type
    gap_up_trades = [t for t in closed if t.signal_type == "gap_up"]
    pullback_trades = [t for t in closed if t.signal_type == "ema_pullback"]

    def _win_rate(ts: List[Trade]) -> float:
        if not ts:
            return 0.0
        return len([t for t in ts if (t.pnl or 0) > 0]) / len(ts)

    return {
        "total_trades": len(closed),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe,
        "avg_hold_days": avg_hold_days,
        "gap_up_trades": len(gap_up_trades),
        "gap_up_win_rate": _win_rate(gap_up_trades),
        "pullback_trades": len(pullback_trades),
        "pullback_win_rate": _win_rate(pullback_trades),
    }
