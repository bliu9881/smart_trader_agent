"""
Order executor for IBKR.
Handles submitting, modifying, and canceling orders.
Supports bracket orders with stop loss and take profit.
"""
import logging
from typing import Optional, Dict, List
from datetime import datetime

from ib_insync import (
    IB, Stock, Order, LimitOrder, MarketOrder, StopOrder,
    BracketOrder, Trade, OrderStatus,
)

from smart_trader.broker.ibkr_client import IBKRClient

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Executes trades through IBKR."""

    def __init__(self, client: IBKRClient):
        self.client = client
        self.ib: IB = client.ib
        self._trade_log: List[Dict] = []

    def submit_market_order(
        self,
        symbol: str,
        quantity: int,
        side: str,  # "BUY" or "SELL"
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Optional[Trade]:
        """Submit a simple market order."""
        if not self.client.ensure_connection():
            logger.error("Cannot submit order: not connected")
            return None

        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)

        order = MarketOrder(side.upper(), quantity)
        order.account = self.client.account_id
        order.tif = "GTC"

        trade = self.ib.placeOrder(contract, order)
        self._log_trade(symbol, side, quantity, "MARKET", trade)

        logger.info(f"Market order submitted: {side} {quantity} {symbol}")
        return trade

    def submit_limit_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        limit_price: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Optional[Trade]:
        """Submit a limit order."""
        if not self.client.ensure_connection():
            return None

        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)

        order = LimitOrder(side.upper(), quantity, limit_price)
        order.account = self.client.account_id
        order.tif = "GTC"

        trade = self.ib.placeOrder(contract, order)
        self._log_trade(symbol, side, quantity, "LIMIT", trade, limit_price=limit_price)

        logger.info(f"Limit order: {side} {quantity} {symbol} @ ${limit_price:.2f}")
        return trade

    def submit_bracket_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Optional[List[Trade]]:
        """
        Submit a bracket order: entry + stop loss + take profit.
        This is the primary order type for the trading system.
        """
        if not self.client.ensure_connection():
            return None

        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)

        bracket = self.ib.bracketOrder(
            action=side.upper(),
            quantity=quantity,
            limitPrice=entry_price,
            takeProfitPrice=take_profit,
            stopLossPrice=stop_loss,
        )

        # Set account and GTC time-in-force for all bracket legs
        # DAY orders get canceled after hours; GTC persists until filled or canceled
        for order in bracket:
            order.account = self.client.account_id
            order.tif = "GTC"

        trades = []
        for order in bracket:
            trade = self.ib.placeOrder(contract, order)
            trades.append(trade)

        self._log_trade(
            symbol, side, quantity, "BRACKET", trades[0] if trades else None,
            limit_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        )

        logger.info(
            f"Bracket order: {side} {quantity} {symbol} "
            f"entry=${entry_price:.2f} SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )
        return trades

    def submit_bracket_with_trailing_stop(
        self,
        symbol: str,
        quantity: int,
        side: str,
        entry_price: float,
        hard_stop: float,
        trailing_percent: float,
        take_profit: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Optional[List[Trade]]:
        """
        Submit entry + hard stop (safety floor) + trailing stop + take profit.

        The trailing stop is IBKR-managed (server-side): the stop price ratchets
        up as the market price rises, never down. `hard_stop` is an absolute
        price floor that only matters for worst-case gap risk at entry.

        `trailing_percent` is percent-from-peak (e.g. 3.0 for 3%).
        """
        if not self.client.ensure_connection():
            return None

        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)

        side_upper = side.upper()
        opposite = "SELL" if side_upper == "BUY" else "BUY"

        # Parent: limit entry order. Children are OCA (one-cancels-all).
        parent = LimitOrder(side_upper, quantity, entry_price)
        parent.account = self.client.account_id
        parent.tif = "GTC"
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False

        # Hard stop (absolute floor) — fills if market gaps below at entry
        hard_stop_order = StopOrder(opposite, quantity, hard_stop)
        hard_stop_order.account = self.client.account_id
        hard_stop_order.tif = "GTC"
        hard_stop_order.parentId = parent.orderId
        hard_stop_order.orderId = self.ib.client.getReqId()
        hard_stop_order.transmit = False
        hard_stop_order.ocaGroup = f"oca_{parent.orderId}"
        hard_stop_order.ocaType = 1  # cancel remaining orders when one fills

        # Trailing stop — server-side trails with price
        trail_order = Order()
        trail_order.action = opposite
        trail_order.totalQuantity = quantity
        trail_order.orderType = "TRAIL"
        trail_order.trailingPercent = trailing_percent
        # Anchor trail start ~1 trail-width below entry so it activates only
        # after price moves favorably by at least one trail increment.
        trail_start_anchor = (
            entry_price * (1 - trailing_percent / 100.0)
            if side_upper == "BUY"
            else entry_price * (1 + trailing_percent / 100.0)
        )
        trail_order.trailStopPrice = round(trail_start_anchor, 2)
        trail_order.account = self.client.account_id
        trail_order.tif = "GTC"
        trail_order.parentId = parent.orderId
        trail_order.orderId = self.ib.client.getReqId()
        trail_order.transmit = False
        trail_order.ocaGroup = f"oca_{parent.orderId}"
        trail_order.ocaType = 1

        # Take profit — last child transmits the whole group
        tp_order = LimitOrder(opposite, quantity, take_profit)
        tp_order.account = self.client.account_id
        tp_order.tif = "GTC"
        tp_order.parentId = parent.orderId
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.transmit = True  # triggers submission of whole group
        tp_order.ocaGroup = f"oca_{parent.orderId}"
        tp_order.ocaType = 1

        trades = [
            self.ib.placeOrder(contract, parent),
            self.ib.placeOrder(contract, hard_stop_order),
            self.ib.placeOrder(contract, trail_order),
            self.ib.placeOrder(contract, tp_order),
        ]

        self._log_trade(
            symbol, side, quantity, "BRACKET_TRAIL", trades[0],
            limit_price=entry_price, stop_loss=hard_stop, take_profit=take_profit,
        )

        logger.info(
            f"Bracket+trail: {side} {quantity} {symbol} "
            f"entry=${entry_price:.2f} hard_stop=${hard_stop:.2f} "
            f"trail={trailing_percent:.1f}% TP=${take_profit:.2f}"
        )
        return trades

    def submit_trailing_stop_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        trailing_percent: float,
        trail_stop_price: Optional[float] = None,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Optional[Trade]:
        """
        Submit a standalone trailing stop exit order (no attached entry).
        Used to add trailing protection to an existing position.

        `trailing_percent` is percent-from-peak (e.g. 3.0 for 3%).
        `trail_stop_price` is the initial stop price; if None, IBKR uses the
        current market price as the anchor.
        """
        if not self.client.ensure_connection():
            return None

        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)

        order = Order()
        order.action = side.upper()
        order.totalQuantity = quantity
        order.orderType = "TRAIL"
        order.trailingPercent = trailing_percent
        if trail_stop_price is not None:
            order.trailStopPrice = round(trail_stop_price, 2)
        order.account = self.client.account_id
        order.tif = "GTC"

        trade = self.ib.placeOrder(contract, order)
        self._log_trade(symbol, side, quantity, "TRAIL", trade)

        logger.info(
            f"Trailing stop: {side} {quantity} {symbol} "
            f"trail={trailing_percent:.1f}%"
            + (f" anchor=${trail_stop_price:.2f}" if trail_stop_price else "")
        )
        return trade

    def cancel_order(self, trade: Trade) -> None:
        """Cancel an open order."""
        if not self.client.ensure_connection():
            return

        self.ib.cancelOrder(trade.order)
        logger.info(f"Cancelled order: {trade.order.orderId}")

    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        if not self.client.ensure_connection():
            return

        self.ib.reqGlobalCancel()
        logger.warning("All orders cancelled")

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel any still-open orders (parent or bracket children) for one symbol.

        Used as a precondition for a signal-driven close so a live bracket
        (stop / trail / take-profit) doesn't race the market sell. Returns
        the number of cancellations issued. A per-trade cancel failure is
        logged but does not abort the loop — best-effort.
        """
        if not self.client.ensure_connection():
            return 0

        target = symbol.upper()
        cancelled = 0
        for trade in self.ib.openTrades():
            try:
                if trade.contract.symbol.upper() != target:
                    continue
                if trade.isDone():
                    continue
                self.ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception as e:
                order_id = getattr(getattr(trade, "order", None), "orderId", "?")
                logger.warning(
                    f"  cancel_orders_for_symbol({symbol}): failed on order {order_id}: {e}"
                )
        if cancelled:
            logger.info(f"Cancelled {cancelled} open order(s) for {symbol}")
        return cancelled

    def close_position(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Optional[Trade]:
        """Close an existing position with a market order."""
        if not self.client.ensure_connection():
            return None

        positions = self.ib.positions(self.client.account_id)
        for pos in positions:
            if pos.contract.symbol == symbol:
                qty = abs(pos.position)
                side = "SELL" if pos.position > 0 else "BUY"
                return self.submit_market_order(symbol, int(qty), side, exchange, currency)

        logger.warning(f"No position found for {symbol}")
        return None

    def close_all_positions(self) -> List[Trade]:
        """Close all open positions."""
        if not self.client.ensure_connection():
            return []

        trades = []
        positions = self.ib.positions(self.client.account_id)

        for pos in positions:
            if pos.position != 0:
                qty = abs(pos.position)
                side = "SELL" if pos.position > 0 else "BUY"
                symbol = pos.contract.symbol
                trade = self.submit_market_order(symbol, int(qty), side)
                if trade:
                    trades.append(trade)

        logger.warning(f"Closing all positions: {len(trades)} orders submitted")
        return trades

    def get_open_orders(self) -> List[Trade]:
        """Get all open orders."""
        if not self.client.ensure_connection():
            return []
        return self.ib.openTrades()

    def get_working_order_symbols(self) -> set:
        """Return the set of symbols that have a working (not-done) order.

        A symbol appears here when it has an order in a live state
        (PendingSubmit / PreSubmitted / Submitted) that has not yet filled or
        cancelled — e.g. an RTH-only limit entry waiting for the regular
        session. The trading loop uses this to avoid resubmitting a duplicate
        entry every cycle for a name whose prior order is still working.

        Best-effort: returns an empty set on connection failure so the caller
        fails open (allows entries) rather than blocking all trading.
        """
        if not self.client.ensure_connection():
            return set()

        symbols: set = set()
        for trade in self.ib.openTrades():
            try:
                if trade.isDone():
                    continue
                symbols.add(trade.contract.symbol.upper())
            except Exception as e:
                order_id = getattr(getattr(trade, "order", None), "orderId", "?")
                logger.warning(
                    f"get_working_order_symbols: skipped order {order_id}: {e}"
                )
        return symbols

    def get_trail_stop_prices(self) -> Dict[str, float]:
        """
        Return the current sell-stop trigger price per symbol.

        Covers both TRAIL orders (IBKR-managed, ratchets up with price via
        `trailStopPrice`) and fixed STP orders (static trigger at `auxPrice`).
        Only SELL-side orders are returned — these are the exits that would
        close a long position if the market drops. For a given symbol with
        multiple active stops, the highest trigger wins (closest to price).
        """
        if not self.client.ensure_connection():
            return {}

        prices: Dict[str, float] = {}
        try:
            for trade in self.ib.openTrades():
                order = trade.order
                if order.action != "SELL":
                    continue

                stop: Optional[float] = None
                if order.orderType == "TRAIL":
                    stop = order.trailStopPrice
                elif order.orderType in ("STP", "STOP"):
                    stop = order.auxPrice

                if stop is None or stop <= 0:
                    continue

                symbol = trade.contract.symbol
                if not symbol:
                    continue

                # Highest active stop wins (closest trigger to current price)
                existing = prices.get(symbol)
                if existing is None or float(stop) > existing:
                    prices[symbol] = float(stop)
        except Exception as e:
            logger.warning(f"Failed to fetch stop prices: {e}")
        return prices

    def _log_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        trade: Optional[Trade],
        limit_price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> None:
        """Log trade for audit trail."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_id": trade.order.orderId if trade else None,
            "status": trade.orderStatus.status if trade else "FAILED",
        }
        self._trade_log.append(entry)

    def get_trade_log(self) -> List[Dict]:
        """Get trade history."""
        return self._trade_log.copy()
