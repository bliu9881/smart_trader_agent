"""EntryCalculator — computes Optimal_Entry_Price for top-N portfolio stocks.

Formula per Requirement 8.2: min(20-day low, 20-day VWAP). Conservative entry.

Only invoked for stocks with in_top_n=True; non-top-N stocks have
optimal_entry_price=None.

Also computes TechnicalSignals for the Humbled Trader primary strategy:
  - 200 SMA trend filter (long only above the line)
  - 8 EMA pullback entry (buy dips to the momentum EMA)
  - Gap up entry (3%+ overnight gap above prior high)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from smart_trader.data.ohlcv_store import OHLCVStore
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)


@dataclass
class TechnicalSignals:
    """Per-symbol technical analysis output for the Humbled Trader strategy."""
    symbol: str
    above_200_sma: bool
    ema_8: Optional[float]
    is_ema_pullback: bool   # price <= ema_8 * (1 + ema_tolerance)
    is_gap_up: bool         # open > prev_close*(1+threshold) AND above 200 SMA
    current_price: Optional[float]
    signal_type: str        # "ema_pullback" | "gap_up" | "both" | "none"


class EntryCalculator:
    def __init__(self, config: SmartMoneyConfig, ohlcv_store: OHLCVStore):
        self.config = config
        self.ohlcv = ohlcv_store

    def compute(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for sym in symbols:
            out[sym] = self._compute_one(sym)
        return out

    def _compute_one(self, symbol: str) -> Optional[float]:
        vwap_days = self.config.entry_vwap_lookback_days
        support_days = self.config.entry_support_lookback_days
        lookback = max(vwap_days, support_days) + 10

        end = datetime.now()
        start = end - timedelta(days=lookback)
        try:
            bars = self.ohlcv.get_or_fetch(
                symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            )
        except Exception as e:
            logger.debug(f"  entry_calc: OHLCV fetch failed for {symbol}: {e}")
            return None
        if bars is None or bars.empty:
            return None

        support = self._nth_day_low(bars, support_days)
        vwap = self._vwap(bars, vwap_days)

        if support is None and vwap is None:
            return None
        if support is None:
            return vwap
        if vwap is None:
            return support
        return min(support, vwap)

    @staticmethod
    def _nth_day_low(bars: pd.DataFrame, n: int) -> Optional[float]:
        if bars.empty:
            return None
        tail = bars.tail(n)
        if tail.empty:
            return None
        return float(tail["low"].min())

    @staticmethod
    def _vwap(bars: pd.DataFrame, n: int) -> Optional[float]:
        if bars.empty:
            return None
        tail = bars.tail(n)
        if tail.empty:
            return None
        typical = (tail["high"] + tail["low"] + tail["close"]) / 3.0
        volume = tail["volume"].astype(float)
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return None
        return float((typical * volume).sum() / total_volume)

    # ----------------------------------------------------------------
    # Technical primary strategy (Humbled Trader)
    # ----------------------------------------------------------------

    def compute_technical(
        self,
        symbols: List[str],
        current_prices: Dict[str, float],
        config: SmartMoneyConfig,
    ) -> Dict[str, TechnicalSignals]:
        """Compute 200 SMA / 8 EMA / gap-up signals for every symbol.

        On fetch failure the symbol gets a safe fail-open default: above_200_sma=True
        with no pullback/gap signal, so it is neither false-triggered nor silently
        blocked by a data outage.
        """
        out: Dict[str, TechnicalSignals] = {}
        for sym in symbols:
            out[sym] = self._compute_technical_one(sym, current_prices.get(sym), config)
        return out

    def _compute_technical_one(
        self,
        symbol: str,
        current_price: Optional[float],
        config: SmartMoneyConfig,
    ) -> TechnicalSignals:
        _fail_open = TechnicalSignals(
            symbol=symbol,
            above_200_sma=True,
            ema_8=None,
            is_ema_pullback=False,
            is_gap_up=False,
            current_price=current_price,
            signal_type="none",
        )

        lookback = max(config.trend_sma_period + 10, 210)
        end = datetime.now()
        start = end - timedelta(days=lookback + 60)  # extra buffer for weekends/holidays
        try:
            bars = self.ohlcv.get_or_fetch(
                symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            )
        except Exception as e:
            logger.debug(f"  entry_calc technical: OHLCV fetch failed for {symbol}: {e}")
            return _fail_open

        if bars is None or len(bars) < config.trend_sma_period:
            logger.debug(
                f"  entry_calc technical: insufficient bars for {symbol} "
                f"({0 if bars is None else len(bars)} < {config.trend_sma_period})"
            )
            return _fail_open

        # Use the latest bar close as price fallback when no live price was passed.
        if current_price is None or current_price <= 0:
            current_price = float(bars.iloc[-1]["close"])

        # 200 SMA — use the last sma_period closes
        sma_200 = float(bars["close"].tail(config.trend_sma_period).mean())
        above_sma = (current_price is not None) and (current_price > sma_200)

        # 8 EMA — exponential moving average of all available closes, take last value
        ema_series = bars["close"].ewm(span=config.ema_period, adjust=False).mean()
        ema_8 = float(ema_series.iloc[-1])

        # EMA pullback: price ≤ ema_8 × (1 + tolerance)
        pullback = (
            current_price is not None
            and current_price <= ema_8 * (1.0 + config.ema_tolerance)
        )

        # Gap up: today's open > prev close × (1 + threshold)
        gap_up = False
        if len(bars) >= 2:
            prev = bars.iloc[-2]
            today = bars.iloc[-1]
            gap_threshold = 1.0 + config.gap_up_threshold
            if today["open"] > prev["close"] * gap_threshold:
                gap_up = True

        if pullback and gap_up:
            sig_type = "both"
        elif pullback:
            sig_type = "ema_pullback"
        elif gap_up:
            sig_type = "gap_up"
        else:
            sig_type = "none"

        return TechnicalSignals(
            symbol=symbol,
            above_200_sma=above_sma,
            ema_8=ema_8,
            is_ema_pullback=pullback,
            is_gap_up=gap_up,
            current_price=current_price,
            signal_type=sig_type,
        )
