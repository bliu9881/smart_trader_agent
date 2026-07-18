"""
Thread-safe state store shared between the trading loop and the FastAPI server.

The trading loop calls `state.update(...)` each cycle. The API endpoints
call `state.snapshot()` to read the latest state.

HMM regime fields are absent — smart-trader has no regime detection.
"""
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List


class StateStore:
    """Thread-safe singleton state shared between trading bot and API."""

    def __init__(self):
        self._lock = Lock()
        self._data: Dict[str, Any] = {
            # Portfolio
            "equity": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "allocation_pct": 0.0,
            "leverage": 1.0,
            # Positions
            "positions": [],
            # Signals
            "recent_signals": [],
            # Risk
            "risk_status": {},
            # System
            "ibkr_connected": False,
            "ibkr_account": "",
            "uptime_seconds": 0.0,
            "last_cycle_seconds": 0.0,
            "next_cycle_time": "",
            "started_at": "",
            # Alerts
            "alert_history": [],
            # Smart money snapshot (top candidates from most recent scan)
            "smart_money_candidates": [],
            # Smart_Money_Portfolio — full-universe snapshot with flags.
            # See api/server.py /api/smart-money-portfolio for the shape.
            "portfolio_snapshot": None,
            # Signal-driven exits proposed this cycle (Phase 1: dry-run only).
            # Each entry: symbol, trigger, conviction_score, sources, actors,
            # reasoning, dry_run, timestamp. See main._evaluate_exit_signals.
            "pending_exits": [],
            # Market regime snapshot (None until the first cycle computes it).
            # Shape: see smart_trader.core.regime.RegimeState.to_dict().
            "regime": None,
        }
        self._started_at = datetime.now()

    def update(self, **kwargs) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._data["uptime_seconds"] = (
                datetime.now() - self._started_at
            ).total_seconds()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def append_signal(self, signal: Dict) -> None:
        """Append a signal to the recent signals list (keep last 50)."""
        with self._lock:
            signals = self._data["recent_signals"]
            signals.append(signal)
            self._data["recent_signals"] = signals[-50:]


# Global singleton — imported by both main.py and server.py
store = StateStore()
