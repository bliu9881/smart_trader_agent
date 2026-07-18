"""
Market data fetching from IBKR and yfinance fallback.
Provides real-time and historical OHLCV data.
"""
import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import yfinance as yf
from ib_insync import IB, Stock, util

from smart_trader.broker.ibkr_client import IBKRClient

logger = logging.getLogger(__name__)


class MarketDataFetcher:
    """Fetches market data from IBKR with yfinance fallback."""

    def __init__(self, client: Optional[IBKRClient] = None):
        self.client = client
        self.ib: Optional[IB] = client.ib if client else None
        self._cache: Dict[str, pd.DataFrame] = {}

    def get_historical_bars(
        self,
        symbol: str,
        duration: str = "2 Y",
        bar_size: str = "1 day",
        use_ibkr: bool = True,
    ) -> pd.DataFrame:
        """
        Get historical OHLCV bars.

        Args:
            symbol: Ticker symbol (e.g., "SPY")
            duration: IBKR duration string (e.g., "2 Y", "6 M", "30 D")
            bar_size: IBKR bar size (e.g., "1 day", "5 mins", "1 hour")
            use_ibkr: Try IBKR first, fallback to yfinance

        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        if use_ibkr and self.client and self.client.is_connected:
            try:
                return self._get_ibkr_bars(symbol, duration, bar_size)
            except Exception as e:
                logger.warning(f"IBKR data failed for {symbol}: {e}, falling back to yfinance")

        return self._get_yfinance_bars(symbol, duration)

    def _get_ibkr_bars(self, symbol: str, duration: str, bar_size: str) -> pd.DataFrame:
        """Fetch historical bars from IBKR."""
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,  # Regular trading hours only
            formatDate=1,
        )

        if not bars:
            raise ValueError(f"No bars returned for {symbol}")

        df = util.df(bars)
        df = df.rename(columns={
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        })

        # Ensure datetime index
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

        df = df[["open", "high", "low", "close", "volume"]]
        logger.info(f"IBKR: Got {len(df)} bars for {symbol} ({bar_size})")
        return df

    def _get_yfinance_bars(self, symbol: str, duration: str) -> pd.DataFrame:
        """Fetch historical bars from yfinance as fallback."""
        # Convert IBKR duration to yfinance period
        period_map = {
            "2 Y": "2y",
            "1 Y": "1y",
            "6 M": "6mo",
            "3 M": "3mo",
            "1 M": "1mo",
            "30 D": "1mo",
            "5 D": "5d",
        }
        period = period_map.get(duration, "2y")

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)

        if df.empty:
            raise ValueError(f"No data returned from yfinance for {symbol}")

        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"

        logger.info(f"yfinance: Got {len(df)} bars for {symbol}")
        return df

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get the latest price for a symbol."""
        if self.client and self.client.is_connected:
            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)
                ticker = self.ib.reqMktData(contract, "", False, False)
                self.ib.sleep(2)
                price = ticker.marketPrice()
                self.ib.cancelMktData(contract)
                if not util.isNan(price):
                    return float(price)
            except Exception as e:
                logger.warning(f"IBKR price failed for {symbol}: {e}")

        # Fallback to yfinance
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.warning(f"yfinance price failed for {symbol}: {e}")

        return None

    def get_latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get latest prices for multiple symbols."""
        prices = {}
        for symbol in symbols:
            price = self.get_latest_price(symbol)
            if price is not None:
                prices[symbol] = price
        return prices

    def get_intraday_bars(
        self,
        symbol: str,
        bar_size: str = "5 mins",
        duration: str = "1 D",
    ) -> pd.DataFrame:
        """Get intraday bars (requires IBKR connection)."""
        if self.client and self.client.is_connected:
            return self._get_ibkr_bars(symbol, duration, bar_size)

        # yfinance fallback for intraday
        interval_map = {
            "1 min": "1m",
            "5 mins": "5m",
            "15 mins": "15m",
            "1 hour": "1h",
        }
        interval = interval_map.get(bar_size, "5m")

        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval=interval)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]
        return df
