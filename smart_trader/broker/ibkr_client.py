"""
Interactive Brokers client wrapper using ib_insync.

Manages connection to TWS/IB Gateway, account info, and paper trading detection.
Requires TWS or IB Gateway to be running with API connections enabled.
"""
import logging
from typing import Optional, Dict

from ib_insync import IB, util

from smart_trader.settings.config import BrokerConfig

logger = logging.getLogger(__name__)


class IBKRClient:
    """Wrapper around ib_insync for IBKR connection management."""

    def __init__(self, config: Optional[BrokerConfig] = None):
        self.config = config or BrokerConfig()
        self.ib = IB()
        self._connected = False
        self._account_id: str = ""
        self._is_paper: bool = True

    def connect(self) -> bool:
        """
        Connect to TWS or IB Gateway.

        Paper trading ports:
          - TWS Paper: 7497
          - IB Gateway Paper: 4002
        Live trading ports:
          - TWS Live: 7496
          - IB Gateway Live: 4001
        """
        try:
            self.ib.connect(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.connection_timeout,
                readonly=self.config.readonly,
            )
            self._connected = True

            # Detect account
            accounts = self.ib.managedAccounts()
            if self.config.account:
                self._account_id = self.config.account
            elif accounts:
                self._account_id = accounts[0]

            # Detect paper trading
            self._is_paper = self._detect_paper_trading()

            logger.info(
                f"Connected to IBKR: account={self._account_id}, "
                f"paper={self._is_paper}, port={self.config.port}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to connect to IBKR: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from IBKR."""
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")

    def _detect_paper_trading(self) -> bool:
        """Detect if connected to paper trading account."""
        # Paper trading ports
        if self.config.port in (7497, 4002):
            return True
        # Paper accounts typically start with 'D' prefix
        if self._account_id.startswith("D"):
            return True
        return False

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    @property
    def account_id(self) -> str:
        return self._account_id

    def get_account_summary(self) -> Dict:
        """Get account summary (equity, buying power, etc.)."""
        if not self.is_connected:
            return {}

        summary = {}
        account_values = self.ib.accountSummary(self._account_id)

        for av in account_values:
            summary[av.tag] = {
                "value": av.value,
                "currency": av.currency,
            }

        return summary

    def get_account_value(self, tag: str = "NetLiquidation") -> float:
        """Get a specific account value."""
        summary = self.get_account_summary()
        if tag in summary:
            try:
                return float(summary[tag]["value"])
            except (ValueError, KeyError):
                return 0.0
        return 0.0

    def get_portfolio_value(self) -> float:
        """Get total portfolio value (net liquidation)."""
        return self.get_account_value("NetLiquidation")

    def get_buying_power(self) -> float:
        """Get available buying power."""
        return self.get_account_value("BuyingPower")

    def is_market_open(self) -> bool:
        """Check if market is currently open."""
        # ib_insync doesn't have a direct market hours check.
        # We check if we can get a live quote for SPY.
        try:
            from ib_insync import Stock
            contract = Stock("SPY", "SMART", "USD")
            self.ib.qualifyContracts(contract)
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)
            is_open = ticker.marketPrice() > 0 and not util.isNan(ticker.marketPrice())
            self.ib.cancelMktData(contract)
            return is_open
        except Exception:
            return False

    def ensure_connection(self) -> bool:
        """Ensure we're connected, reconnect if needed."""
        if self.is_connected:
            return True
        logger.warning("Connection lost, attempting reconnect...")
        return self.connect()
