"""Tests for OrderExecutor helpers that don't require a live IBKR connection.

These mock the `ib` handle and the IBKRClient so the broker layer can be
exercised in the test runner (main thread) without ib_insync's event loop.
"""
from unittest.mock import MagicMock

from smart_trader.broker.order_executor import OrderExecutor


def _mk_trade(symbol: str, done: bool):
    trade = MagicMock()
    trade.contract.symbol = symbol
    trade.isDone.return_value = done
    return trade


def _executor_with_trades(trades):
    client = MagicMock()
    client.ensure_connection.return_value = True
    ex = OrderExecutor(client)
    ex.ib = MagicMock()
    ex.ib.openTrades.return_value = trades
    return ex


class TestGetWorkingOrderSymbols:
    def test_excludes_done_trades(self):
        ex = _executor_with_trades([
            _mk_trade("MSFT", done=False),   # working
            _mk_trade("AAPL", done=True),    # filled/cancelled → excluded
        ])
        assert ex.get_working_order_symbols() == {"MSFT"}

    def test_dedupes_case_insensitively(self):
        ex = _executor_with_trades([
            _mk_trade("MSFT", done=False),
            _mk_trade("msft", done=False),   # same name, different case
            _mk_trade("NVDA", done=False),
        ])
        assert ex.get_working_order_symbols() == {"MSFT", "NVDA"}

    def test_empty_when_no_open_trades(self):
        ex = _executor_with_trades([])
        assert ex.get_working_order_symbols() == set()

    def test_fails_open_on_connection_failure(self):
        """Connection failure → empty set so the caller still allows entries."""
        client = MagicMock()
        client.ensure_connection.return_value = False
        ex = OrderExecutor(client)
        ex.ib = MagicMock()
        assert ex.get_working_order_symbols() == set()
        ex.ib.openTrades.assert_not_called()

    def test_one_bad_trade_does_not_abort(self):
        """A trade that raises on inspection is skipped, others still returned."""
        good = _mk_trade("NVDA", done=False)
        bad = MagicMock()
        type(bad).contract = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        bad.isDone.return_value = False
        ex = _executor_with_trades([bad, good])
        assert ex.get_working_order_symbols() == {"NVDA"}
