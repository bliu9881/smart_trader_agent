"""
Ladder-In Engine — dollar-cost averaging on dips for held positions.

When a held position's market price drops past configurable thresholds from
the original entry price, the engine generates additional BUY signals at
fixed share sizes.

Decoupled from the HMM regime layer. smart-trader has no regime detection,
so there's no vol_rank gate — thresholds apply uniformly. If a future VIX
or SMA-200 filter is added, it should gate upstream (in main.py), not here.

Defaults (from LadderInConfig):
  -15% → +10 shares, -25% → +20 shares.

All signals are standard Signal objects that flow through
RiskManager.validate_signal() just like smart-money entries.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Set

import pandas as pd

from smart_trader.core.signal import Signal
from smart_trader.settings.config import LadderInConfig

logger = logging.getLogger(__name__)


class LadderInEngine:
    """
    Evaluates held positions against ladder-in levels and generates
    additional BUY signals when price-drop thresholds are met.
    """

    def __init__(
        self,
        config: LadderInConfig,
        default_stop_pct: float = 0.08,
        default_trail_pct: float = 0.05,
    ):
        self.config = config
        self.default_stop_pct = default_stop_pct
        self.default_trail_pct = default_trail_pct
        # symbol → set of triggered level indices (reset on position close / restart)
        self._triggered: Dict[str, Set[int]] = {}

        # Validate config at init
        if len(config.thresholds) != len(config.shares):
            logger.warning(
                f"LadderInConfig: thresholds ({len(config.thresholds)}) and "
                f"shares ({len(config.shares)}) have different lengths; "
                f"using min({len(config.thresholds)}, {len(config.shares)}) levels"
            )
        for i, t in enumerate(config.thresholds):
            if t >= 0:
                logger.warning(
                    f"LadderInConfig: thresholds[{i}] = {t} is non-negative "
                    f"and will be ignored (thresholds must be < 0)"
                )

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate_positions(
        self,
        positions: Dict[str, Dict[str, Any]],
        bars_by_symbol: Dict[str, pd.DataFrame],
    ) -> List[Signal]:
        """
        Check all held positions against ladder levels.
        Returns a list of ladder-in BUY signals (may be empty).
        """
        if not self.config.ladder_in_enabled:
            return []

        thresholds = self.config.thresholds
        shares_list = self.config.shares
        n = min(len(thresholds), len(shares_list))
        thresholds = thresholds[:n]
        shares_list = shares_list[:n]
        if not thresholds:
            return []

        signals: List[Signal] = []

        for symbol, pos in positions.items():
            avg_cost = pos.get("avg_cost", 0)
            if avg_cost <= 0:
                logger.warning(f"Ladder-in: skipping {symbol} — avg_cost={avg_cost}")
                continue

            quantity = pos.get("quantity", 0)
            if quantity == 0:
                continue

            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars) < 50:
                logger.debug(f"Ladder-in: skipping {symbol} — insufficient bars")
                continue

            market_price = pos.get("market_price", 0)
            if market_price <= 0:
                market_price = float(bars["close"].iloc[-1])

            drop_pct = (market_price - avg_cost) / avg_cost

            triggered_set = self._triggered.setdefault(symbol, set())

            for level_idx, (threshold, add_shares) in enumerate(zip(thresholds, shares_list)):
                if threshold >= 0:
                    continue
                if level_idx in triggered_set:
                    continue

                if drop_pct <= threshold:
                    # Without a strategy to derive stop_loss, use a default %
                    # below the current market price. The hard stop is a safety
                    # floor; the trailing_stop_pct is the active exit mechanism.
                    stop_loss = market_price * (1 - self.default_stop_pct)

                    sig = Signal(
                        symbol=symbol,
                        direction="LONG",
                        confidence=1.0,  # ladder-in triggers are deterministic
                        entry_price=market_price,
                        stop_loss=stop_loss,
                        trailing_stop_pct=self.default_trail_pct,
                        position_size_pct=0.0,  # Not used — shares are fixed below
                        leverage=1.0,
                        timestamp=datetime.now(),
                        reasoning=(
                            f"Ladder-in L{level_idx}: {symbol} dropped {drop_pct:.1%} "
                            f"from entry ${avg_cost:.2f}, adding {add_shares} shares"
                        ),
                        strategy_name="LadderIn",
                        metadata={
                            "ladder_in": True,
                            "ladder_level": level_idx,
                            "original_entry_price": avg_cost,
                            "drop_pct": drop_pct,
                            "additional_shares": add_shares,
                        },
                    )
                    signals.append(sig)
                    triggered_set.add(level_idx)

                    logger.info(
                        f"Ladder-in TRIGGERED: {symbol} L{level_idx} "
                        f"price=${market_price:.2f} entry=${avg_cost:.2f} "
                        f"drop={drop_pct:.1%} → +{add_shares} shares"
                    )
                else:
                    logger.debug(
                        f"Ladder-in: {symbol} L{level_idx} not triggered "
                        f"(drop={drop_pct:.1%}, threshold={threshold:.0%})"
                    )

        return signals

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def clear_symbol(self, symbol: str) -> None:
        """Clear triggered-level state when a position is fully closed."""
        self._triggered.pop(symbol, None)
        logger.info(f"Ladder-in: cleared state for {symbol}")

    def get_triggered_levels(self, symbol: str) -> Set[int]:
        """Return the set of triggered level indices for a symbol."""
        return self._triggered.get(symbol, set()).copy()
