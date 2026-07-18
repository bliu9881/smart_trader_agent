"""
Position tracker for monitoring open positions via IBKR.
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime

from ib_insync import IB, Position

from smart_trader.broker.ibkr_client import IBKRClient

logger = logging.getLogger(__name__)


class PositionTracker:
    """Tracks open positions and P&L from IBKR."""

    def __init__(self, client: IBKRClient):
        self.client = client
        self.ib: IB = client.ib
        self._position_history: List[Dict] = []

    def get_positions(self) -> Dict[str, Dict]:
        """
        Get all open positions.

        Returns dict: symbol → {quantity, avg_cost, market_value, unrealized_pnl, ...}
        """
        if not self.client.ensure_connection():
            return {}

        positions = {}
        portfolio_items = self.ib.portfolio(self.client.account_id)

        for item in portfolio_items:
            symbol = item.contract.symbol
            positions[symbol] = {
                "symbol": symbol,
                "quantity": item.position,
                "avg_cost": item.averageCost,
                "market_price": item.marketPrice,
                "market_value": item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "realized_pnl": item.realizedPNL,
                "contract": item.contract,
            }

        return positions

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get position for a specific symbol."""
        positions = self.get_positions()
        return positions.get(symbol)

    def get_allocation_fractions(self, portfolio_value: float) -> Dict[str, float]:
        """
        Get current allocation as fraction of portfolio for each symbol.

        Returns dict: symbol → fraction (0.0 - 1.0)
        """
        if portfolio_value <= 0:
            return {}

        positions = self.get_positions()
        allocations = {}

        for symbol, pos in positions.items():
            market_value = abs(pos["market_value"])
            allocations[symbol] = market_value / portfolio_value

        return allocations

    def get_total_unrealized_pnl(self) -> float:
        """Get total unrealized P&L across all positions."""
        positions = self.get_positions()
        return sum(pos["unrealized_pnl"] for pos in positions.values())

    def get_total_realized_pnl(self) -> float:
        """Get total realized P&L across all positions."""
        positions = self.get_positions()
        return sum(pos["realized_pnl"] for pos in positions.values())

    def get_position_count(self) -> int:
        """Get number of open positions."""
        return len(self.get_positions())

    def snapshot(self) -> Dict:
        """Take a snapshot of current positions for logging."""
        positions = self.get_positions()
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "position_count": len(positions),
            "total_unrealized_pnl": self.get_total_unrealized_pnl(),
            "positions": {
                symbol: {
                    "qty": pos["quantity"],
                    "avg_cost": pos["avg_cost"],
                    "market_price": pos["market_price"],
                    "pnl": pos["unrealized_pnl"],
                }
                for symbol, pos in positions.items()
            },
        }
        self._position_history.append(snapshot)
        return snapshot

    def get_position_history(self) -> List[Dict]:
        """Get historical position snapshots."""
        return self._position_history.copy()
