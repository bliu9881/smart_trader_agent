"""
Signal dataclass — the trading order envelope.

Carried unchanged from regime_trader/core/regime_strategies.py (lines 36-58),
but with the HMM-specific fields (regime_id, regime_name, regime_probability)
removed because smart-trader has no regime detection layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Signal:
    """Trading signal emitted by any source (smart money, ladder-in, manual)."""
    symbol: str
    direction: str  # "LONG" or "FLAT"
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: Optional[float] = None
    position_size_pct: float = 0.0    # Fraction of portfolio
    leverage: float = 1.0             # 1.0 = no leverage
    # Regime-adaptive trailing stop (IBKR-managed). When set, the executor
    # uses a TRAIL order instead of a fixed stop. Floor ratchets up as price
    # makes new highs, never down. Hard `stop_loss` remains as the initial
    # safety floor for gap risk.
    trailing_stop_pct: Optional[float] = None
    timestamp: Optional[datetime] = None
    reasoning: str = ""
    strategy_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
