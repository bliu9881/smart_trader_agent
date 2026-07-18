"""
Mock broker — drop-in replacement for IBKR broker components.

Used when the IBKR Gateway is unreachable (hackathon demo deployment).
Fills orders instantly at the most recent closing price from OHLCVStore,
or $100.00 if no OHLCV data exists for the symbol.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from smart_trader.core.signal import Signal

logger = logging.getLogger(__name__)

# Default fill price when no OHLCV data is available.
_DEFAULT_FILL_PRICE = 100.00

# Starting simulated equity.
_INITIAL_EQUITY = 100_000.00


def _duration_to_days(duration: str) -> int:
    """Convert an IBKR-style duration ("2 Y", "6 M", "30 D") to days."""
    try:
        num_str, unit = duration.strip().split()
        num = int(num_str)
    except (ValueError, AttributeError):
        return 365
    u = unit.upper()
    if u.startswith("Y"):
        return num * 365
    if u.startswith("M"):
        return num * 31
    if u.startswith("W"):
        return num * 7
    return num


class MockBroker:
    """Simulates broker operations using OHLCV data for fills."""

    def __init__(self, ohlcv_store=None):
        """
        Args:
            ohlcv_store: Optional OHLCVStore instance for looking up recent
                close prices. If None, all fills use the default price.
        """
        self._ohlcv_store = ohlcv_store
        self._initial_equity = _INITIAL_EQUITY
        self._cash = _INITIAL_EQUITY
        # symbol → {"shares": int, "avg_price": float, "market_value": float}
        self._positions: Dict[str, dict] = {}
        self._fills: list = []
        # symbol → set of working order IDs (simplified: just track symbols)
        self._working_orders: Dict[str, list] = {}
        self._next_order_id = 1

    # ------------------------------------------------------------------ duck-type compat
    # These properties/methods make MockBroker a drop-in replacement for
    # IBKRClient, OrderExecutor, PositionTracker, and MarketDataFetcher
    # in the main trading loop.

    @property
    def is_paper(self) -> bool:
        return True

    @property
    def account_id(self) -> str:
        return "MOCK_DEMO"

    @property
    def is_connected(self) -> bool:
        return True

    def ensure_connection(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def get_trail_stop_prices(self) -> Dict[str, float]:
        """No trailing stops in mock mode."""
        return {}

    def submit_bracket_order(
        self, symbol: str, shares: int, action: str, **kwargs
    ) -> dict:
        """Wrap submit_order for bracket-order interface compatibility."""
        sig = Signal(
            symbol=symbol,
            direction="LONG" if action == "BUY" else "FLAT",
            confidence=1.0,
            entry_price=kwargs.get("entry_price", _DEFAULT_FILL_PRICE),
            stop_loss=kwargs.get("stop_loss", 0.0),
            take_profit=kwargs.get("take_profit", 0.0),
            trailing_stop_pct=0.0,
            position_size_pct=0.0,
            leverage=1.0,
            timestamp=datetime.now(),
            reasoning="mock bracket order",
            strategy_name="Mock",
            metadata={"mock_shares_override": shares},
        )
        return self._fill_with_shares(sig, shares)

    def submit_bracket_with_trailing_stop(
        self, symbol: str, shares: int, action: str, **kwargs
    ) -> dict:
        """Wrap submit_order for trailing-stop bracket interface."""
        return self.submit_bracket_order(symbol, shares, action, **kwargs)

    def get_historical_bars(self, symbol: str, duration: str = "2 Y",
                            bar_size: str = "1 day", **kwargs):
        """Serve daily OHLCV bars from the (yfinance-backed) OHLCVStore.

        This lets the market-regime gate and ladder-in logic work in mock mode.
        Returns a DataFrame with columns open/high/low/close/volume, or None if
        no store is wired or no data is available. Only daily bars are supported
        (the store is a daily cache); intraday bar_size requests fall through to
        the same daily series, which is fine for the regime/ladder use cases.
        """
        if self._ohlcv_store is None:
            return None
        try:
            days = _duration_to_days(duration)
            end = datetime.now()
            start = end - timedelta(days=days)
            df = self._ohlcv_store.get_or_fetch(
                symbol,
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
            )
            if df is None or df.empty:
                return None
            return df
        except Exception as e:
            logger.warning(
                f"MockBroker: get_historical_bars failed for {symbol}: {e}"
            )
            return None

    def set_ohlcv_store(self, ohlcv_store) -> None:
        """Allow late-binding of the OHLCV store after initialization."""
        self._ohlcv_store = ohlcv_store

    # ------------------------------------------------------------------ orders

    def _fill_with_shares(self, signal: Signal, shares: int) -> dict:
        """Fill a given number of shares (used by bracket-order compat methods)."""
        fill_price = self._get_fill_price(signal.symbol)

        if shares <= 0:
            return {
                "order_id": None,
                "status": "rejected",
                "reason": "zero_shares",
                "symbol": signal.symbol,
            }

        if signal.direction == "LONG":
            self._buy(signal.symbol, shares, fill_price)
        else:
            self._sell(signal.symbol, shares, fill_price)

        order_id = self._next_order_id
        self._next_order_id += 1

        fill_record = {
            "order_id": order_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "shares": shares,
            "fill_price": fill_price,
            "timestamp": datetime.now().isoformat(),
            "status": "filled",
        }
        self._fills.append(fill_record)

        logger.info(
            f"MockBroker FILL: {signal.direction} {shares} {signal.symbol} "
            f"@ ${fill_price:.2f}"
        )
        return fill_record

    def submit_order(self, signal: Signal) -> dict:
        """Fill instantly at last close price, or $100 if no data."""
        fill_price = self._get_fill_price(signal.symbol)
        shares = self._compute_shares(signal, fill_price)

        if shares == 0:
            logger.warning(
                f"MockBroker: 0 shares for {signal.symbol} "
                f"(size_pct={signal.position_size_pct}, price={fill_price})"
            )
            return {
                "order_id": None,
                "status": "rejected",
                "reason": "zero_shares",
                "symbol": signal.symbol,
            }

        # Execute the fill
        if signal.direction == "LONG":
            self._buy(signal.symbol, shares, fill_price)
        else:
            # FLAT direction means close/sell
            self._sell(signal.symbol, shares, fill_price)

        order_id = self._next_order_id
        self._next_order_id += 1

        fill_record = {
            "order_id": order_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "shares": shares,
            "fill_price": fill_price,
            "timestamp": datetime.now().isoformat(),
            "status": "filled",
        }
        self._fills.append(fill_record)

        logger.info(
            f"MockBroker FILL: {signal.direction} {shares} {signal.symbol} "
            f"@ ${fill_price:.2f}"
        )

        return fill_record

    # ------------------------------------------------------------------ positions

    def get_positions(self) -> Dict[str, dict]:
        """Return in-memory position state."""
        return {
            sym: {
                "symbol": sym,
                "quantity": pos["shares"],
                "avg_cost": pos["avg_price"],
                "market_price": pos["avg_price"],  # simplified: no live quotes
                "market_value": pos["market_value"],
                "unrealized_pnl": 0.0,  # no live price movement in mock
                "realized_pnl": 0.0,
            }
            for sym, pos in self._positions.items()
            if pos["shares"] > 0
        }

    # ------------------------------------------------------------------ portfolio

    def get_portfolio_value(self) -> float:
        """Return simulated equity (initial cash + position values)."""
        position_value = sum(
            pos["market_value"] for pos in self._positions.values()
            if pos["shares"] > 0
        )
        return self._cash + position_value

    def get_buying_power(self) -> float:
        """Return cash available (not invested in positions)."""
        return self._cash

    # ------------------------------------------------------------------ order management

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel any working orders for a symbol. Returns count cancelled."""
        symbol_upper = symbol.upper()
        orders = self._working_orders.pop(symbol_upper, [])
        cancelled = len(orders)
        if cancelled:
            logger.info(f"MockBroker: cancelled {cancelled} order(s) for {symbol}")
        return cancelled

    def close_position(self, symbol: str) -> Optional[dict]:
        """Close an existing position with a market sell."""
        symbol_upper = symbol.upper()
        pos = self._positions.get(symbol_upper)
        if pos is None or pos["shares"] <= 0:
            logger.warning(f"MockBroker: no position to close for {symbol}")
            return None

        shares = pos["shares"]
        fill_price = self._get_fill_price(symbol_upper)
        self._sell(symbol_upper, shares, fill_price)

        order_id = self._next_order_id
        self._next_order_id += 1

        fill_record = {
            "order_id": order_id,
            "symbol": symbol_upper,
            "direction": "FLAT",
            "shares": shares,
            "fill_price": fill_price,
            "timestamp": datetime.now().isoformat(),
            "status": "filled",
        }
        self._fills.append(fill_record)

        logger.info(
            f"MockBroker CLOSE: SELL {shares} {symbol_upper} @ ${fill_price:.2f}"
        )
        return fill_record

    def get_working_order_symbols(self) -> set:
        """Return set of symbols with working (unfilled) orders."""
        return set(self._working_orders.keys())

    # ------------------------------------------------------------------ internals

    def _get_fill_price(self, symbol: str) -> float:
        """Get the most recent close price, or default $100."""
        if self._ohlcv_store is None:
            return _DEFAULT_FILL_PRICE

        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            df = self._ohlcv_store.get_or_fetch(symbol, start, end)
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        except Exception as e:
            logger.warning(
                f"MockBroker: failed to get price for {symbol}: {e}, "
                f"using default ${_DEFAULT_FILL_PRICE}"
            )

        return _DEFAULT_FILL_PRICE

    def _compute_shares(self, signal: Signal, fill_price: float) -> int:
        """Compute share count from signal's position_size_pct."""
        if fill_price <= 0:
            return 0

        # Use position_size_pct of current equity to determine dollar amount.
        # If position_size_pct is 0, use a default 2% of equity.
        size_pct = signal.position_size_pct if signal.position_size_pct > 0 else 0.02
        dollar_amount = self.get_portfolio_value() * size_pct
        shares = int(dollar_amount / fill_price)
        return max(shares, 0)

    def _buy(self, symbol: str, shares: int, price: float) -> None:
        """Record a buy fill."""
        symbol = symbol.upper()
        cost = shares * price

        if symbol in self._positions and self._positions[symbol]["shares"] > 0:
            # Average into existing position
            existing = self._positions[symbol]
            total_shares = existing["shares"] + shares
            total_cost = (existing["avg_price"] * existing["shares"]) + cost
            self._positions[symbol] = {
                "shares": total_shares,
                "avg_price": total_cost / total_shares,
                "market_value": total_shares * price,
            }
        else:
            self._positions[symbol] = {
                "shares": shares,
                "avg_price": price,
                "market_value": shares * price,
            }

        self._cash -= cost

    def _sell(self, symbol: str, shares: int, price: float) -> None:
        """Record a sell fill."""
        symbol = symbol.upper()
        pos = self._positions.get(symbol)
        if pos is None or pos["shares"] <= 0:
            return

        shares_to_sell = min(shares, pos["shares"])
        proceeds = shares_to_sell * price
        remaining = pos["shares"] - shares_to_sell

        if remaining > 0:
            self._positions[symbol] = {
                "shares": remaining,
                "avg_price": pos["avg_price"],
                "market_value": remaining * price,
            }
        else:
            self._positions[symbol] = {
                "shares": 0,
                "avg_price": 0.0,
                "market_value": 0.0,
            }

        self._cash += proceeds
