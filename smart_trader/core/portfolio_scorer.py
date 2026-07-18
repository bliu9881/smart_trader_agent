"""PortfolioScorer — computes Composite_Score for every stock held by at
least one Holdings_Provider.

Five weighted components, all normalized to 0-to-1:
  - overlap        = overlap_count / n_providers
  - holding_weight = mean(per-fund Holding_Weight), clamped/scaled to [0,1]
  - performance    = rank of 6-month total return, cross-sectional
  - momentum       = rank of mean(1m, 3m, 6m ROC), cross-sectional
  - relative_strength = rank of mean(stock_return - SPY_return) over 1/3/6m

Stocks without price history get Performance = Momentum = RS = 0 and are
scored on overlap + holding weight only.

Output: List[ScoredStock] sorted by composite_score desc, with Momentum_Score
as tiebreaker when two stocks are within 5% of each other (see Requirement 9.4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from smart_trader.core.smart_money import FundHoldings
from smart_trader.data.ohlcv_store import OHLCVStore
from smart_trader.settings.config import SmartMoneyConfig

logger = logging.getLogger(__name__)


@dataclass
class FundHoldingRef:
    """Per-fund view used for dashboard/API per-stock detail."""
    fund_name: str
    provider_name: str
    holding_weight: float
    share_count: int
    market_value: float
    as_of_date: datetime


@dataclass
class ScoredStock:
    """A stock with its composite score, component scores, and fund list."""
    symbol: str
    composite_score: float
    overlap_count: int
    average_holding_weight: float
    performance_score: float
    momentum_score: float
    relative_strength: float
    funds: List[FundHoldingRef] = field(default_factory=list)


class PortfolioScorer:
    def __init__(self, config: SmartMoneyConfig, ohlcv_store: OHLCVStore):
        self.config = config
        self.ohlcv = ohlcv_store

    def score(self, holdings: List[FundHoldings], n_providers: int) -> List[ScoredStock]:
        if not holdings or n_providers <= 0:
            return []

        # --- Aggregate fund holdings per symbol ---
        per_symbol: Dict[str, List[FundHoldings]] = {}
        for h in holdings:
            per_symbol.setdefault(h.symbol, []).append(h)

        symbols = sorted(per_symbol.keys())

        # --- Raw component values ---
        overlap: Dict[str, float] = {}
        avg_weight: Dict[str, float] = {}
        for sym, items in per_symbol.items():
            # Count distinct providers holding the stock
            providers = {h.provider_name for h in items}
            overlap[sym] = len(providers)
            avg_weight[sym] = sum(h.holding_weight for h in items) / len(items)

        # --- Performance / momentum / RS from OHLCV ---
        perf_raw: Dict[str, Optional[float]] = {}
        mom_raw: Dict[str, Optional[float]] = {}
        rs_raw: Dict[str, Optional[float]] = {}

        spy_bars = self._load_bars("SPY", self.config.performance_lookback_days + 10)
        spy_returns = self._multi_period_returns(spy_bars)

        for sym in symbols:
            bars = self._load_bars(sym, self.config.performance_lookback_days + 10)
            if bars is None or bars.empty:
                perf_raw[sym] = mom_raw[sym] = rs_raw[sym] = None
                continue
            # 6-month return
            perf_raw[sym] = self._period_return(bars, 180)
            # Momentum: mean of 1m, 3m, 6m ROC
            sym_returns = self._multi_period_returns(bars)
            if sym_returns:
                mom_raw[sym] = sum(sym_returns.values()) / len(sym_returns)
                # Relative strength vs SPY
                if spy_returns and all(p in spy_returns for p in sym_returns):
                    rs_raw[sym] = sum(
                        sym_returns[p] - spy_returns[p] for p in sym_returns
                    ) / len(sym_returns)
                else:
                    rs_raw[sym] = None
            else:
                mom_raw[sym] = None
                rs_raw[sym] = None

        # --- Cross-sectional rank normalization (0..1) ---
        perf_norm = _rank_normalize(perf_raw)
        mom_norm = _rank_normalize(mom_raw)
        rs_norm = _rank_normalize(rs_raw)

        # --- Composite_Score ---
        results: List[ScoredStock] = []
        for sym in symbols:
            overlap_norm = overlap[sym] / max(1, n_providers)
            # Holding_Weight is already in [0..1] (fund portfolio fraction).
            # Clamp defensively in case bad source data.
            hw = min(1.0, max(0.0, avg_weight[sym]))
            p = perf_norm.get(sym, 0.0)
            m = mom_norm.get(sym, 0.0)
            r = rs_norm.get(sym, 0.0)

            composite = (
                self.config.overlap_weight * overlap_norm
                + self.config.holding_weight_weight * hw
                + self.config.performance_weight * p
                + self.config.momentum_weight * m
                + self.config.relative_strength_weight * r
            )

            funds = [
                FundHoldingRef(
                    fund_name=h.fund_name,
                    provider_name=h.provider_name,
                    holding_weight=h.holding_weight,
                    share_count=h.share_count,
                    market_value=h.market_value,
                    as_of_date=h.as_of_date,
                )
                for h in per_symbol[sym]
            ]

            results.append(ScoredStock(
                symbol=sym,
                composite_score=composite,
                overlap_count=int(overlap[sym]),
                average_holding_weight=hw,
                performance_score=p,
                momentum_score=m,
                relative_strength=r,
                funds=funds,
            ))

        # Sort descending. Tiebreaker (Req 9.4): when two scores are within 5%,
        # the higher momentum_score comes first. Stable sort preserves the
        # primary order; re-sort with a custom key that sums composite and
        # a small momentum nudge only within the 5%-window via groupby.
        results.sort(key=lambda s: s.composite_score, reverse=True)
        _apply_momentum_tiebreak(results, epsilon=0.05)
        return results

    # ------------------------------------------------------------------ helpers

    def _load_bars(self, symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
        end = datetime.now()
        start = end - timedelta(days=lookback_days + 10)
        try:
            df = self.ohlcv.get_or_fetch(
                symbol,
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.debug(f"  scorer: OHLCV fetch failed for {symbol}: {e}")
            return None
        if df is None or df.empty:
            return None
        return df

    def _period_return(self, bars: pd.DataFrame, days: int) -> Optional[float]:
        """Total return from `days` ago to now."""
        if bars is None or bars.empty:
            return None
        if len(bars) < 2:
            return None
        cutoff = bars.index[-1] - pd.Timedelta(days=days)
        past = bars[bars.index <= cutoff]
        if past.empty:
            # Not enough history for this horizon
            return None
        start_price = float(past["close"].iloc[-1])
        end_price = float(bars["close"].iloc[-1])
        if start_price <= 0:
            return None
        return (end_price - start_price) / start_price

    def _multi_period_returns(self, bars: pd.DataFrame) -> Dict[str, float]:
        """Return 1m/3m/6m returns for a symbol. Skips periods with insufficient history."""
        out: Dict[str, float] = {}
        for label, days in (("1m", 30), ("3m", 90), ("6m", 180)):
            r = self._period_return(bars, days)
            if r is not None:
                out[label] = r
        return out


def _rank_normalize(raw: Dict[str, Optional[float]]) -> Dict[str, float]:
    """Rank-based normalization to [0,1]. Missing values score 0."""
    valid = {k: v for k, v in raw.items() if v is not None}
    if not valid:
        return {k: 0.0 for k in raw}
    # Sort ascending; highest raw → rank 1.0
    ordered = sorted(valid.items(), key=lambda kv: kv[1])
    n = len(ordered)
    out: Dict[str, float] = {k: 0.0 for k in raw}
    if n == 1:
        out[ordered[0][0]] = 0.5
        return out
    for i, (k, _) in enumerate(ordered):
        out[k] = i / (n - 1)
    return out


def _apply_momentum_tiebreak(stocks: List[ScoredStock], epsilon: float) -> None:
    """Reorder in-place: within any 5%-score cluster, higher momentum first.

    Walk through the list and group adjacent stocks whose scores are within
    `epsilon` of the group leader; sort each group by momentum_score desc.
    """
    i = 0
    n = len(stocks)
    while i < n:
        j = i + 1
        leader_score = stocks[i].composite_score
        while j < n:
            denom = max(abs(leader_score), abs(stocks[j].composite_score), 1e-9)
            if abs(leader_score - stocks[j].composite_score) / denom < epsilon:
                j += 1
            else:
                break
        if j - i > 1:
            cluster = stocks[i:j]
            cluster.sort(key=lambda s: s.momentum_score, reverse=True)
            stocks[i:j] = cluster
        i = j
