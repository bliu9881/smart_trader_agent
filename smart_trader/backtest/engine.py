"""BacktestEngine — historical replay of the Humbled Trader technical strategy.

Fetches OHLCV directly from yfinance (bypasses OHLCVStore/Supabase) so that
the backtest works without a live database connection.

Signal logic mirrors entry_calculator.py and trading_filter.py:
  - 200-day SMA trend filter (long only above)
  - 8-period EMA pullback entry (fill next-day open)
  - 2%+ overnight gap up entry (fill same-day open)

Exits use fixed levels from TraderConfig:
  - Hard stop: entry × (1 − default_stop_pct)
  - Take profit: entry × (1 + default_take_profit_pct)
  - Trailing stop: updated daily at close, triggers on low

Optional features controlled via run() kwargs:
  - gap_threshold  — tune gap-up sensitivity (prev_high check permanently removed)
  - trail_pct                              — override trailing stop width
  - regime_filter / regime_symbol / regime_symbol_ca / regime_sma_period
        — per-country market regime gate. US symbols are gated by regime_symbol
        (default SPY); Canadian symbols (.TO / .V suffix) by regime_symbol_ca
        (default ^GSPTSE). Each symbol is checked against its own country's proxy.

CatalystAnalyzer and SmartMoneyScanner are excluded — historical news is
unreliable via yfinance and filings data is not point-in-time.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from smart_trader.backtest.metrics import compute_metrics
from smart_trader.backtest.portfolio import SimulatedPortfolio, Trade
from smart_trader.settings.config import AppConfig, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    trades: List[Trade]
    equity_curve: List[float]
    start: str
    end: str
    initial_equity: float
    symbols: List[str]
    regime_filter: bool = False
    regime_symbol: str = "SPY"
    regime_symbol_ca: str = "^GSPTSE"
    regime_sma_label: str = "50"   # e.g. "50" or "50+200"
    slippage_bps: float = 0.0
    commission_per_share: float = 0.0

    def metrics(self) -> Dict:
        return compute_metrics(self.trades, self.equity_curve)

    def print_summary(self) -> None:
        m = self.metrics()
        if "error" in m:
            print(f"Backtest produced no results: {m['error']}")
            return

        print(f"\n{'='*55}")
        print(f"  Backtest: {self.start} → {self.end}")
        print(f"{'='*55}")
        print(f"  Symbols:        {', '.join(self.symbols[:6])}{'...' if len(self.symbols)>6 else ''}")
        if self.regime_filter:
            if any(_is_canadian(s) for s in self.symbols):
                print(f"  Regime filter:  ON (US:{self.regime_symbol} / "
                      f"CA:{self.regime_symbol_ca}, {self.regime_sma_label}-SMA)")
            else:
                print(f"  Regime filter:  ON ({self.regime_symbol} "
                      f"{self.regime_sma_label}-SMA)")
        if self.slippage_bps or self.commission_per_share:
            total_comm = sum(
                (t.entry_commission or 0.0) + (t.exit_commission or 0.0)
                for t in self.trades
            )
            print(f"  Costs:          {self.slippage_bps:g} bps slip + "
                  f"${self.commission_per_share:g}/sh comm "
                  f"(${total_comm:,.0f} total commission)")
        else:
            print("  Costs:          NONE (gross / frictionless)")
        print(f"  Initial equity: ${self.initial_equity:,.0f}")
        if self.equity_curve:
            print(f"  Final equity:   ${self.equity_curve[-1]:,.0f}")
        print()
        print(f"  Total trades:   {m['total_trades']}")
        print(f"    Gap up:       {m['gap_up_trades']} (win {m['gap_up_win_rate']:.0%})")
        print(f"    EMA pullback: {m['pullback_trades']} (win {m['pullback_win_rate']:.0%})")
        print()
        print(f"  Win rate:       {m['win_rate']:.1%}")
        print(f"  Profit factor:  {m['profit_factor']:.2f}")
        print(f"  Avg win:        ${m['avg_win']:+,.0f}")
        print(f"  Avg loss:       ${m['avg_loss']:+,.0f}")
        print(f"  Avg hold:       {m['avg_hold_days']:.1f} days")
        print()
        print(f"  Total P&L:      ${m['total_pnl']:+,.0f}")
        print(f"  Total return:   {m['total_return']:+.1%}")
        print(f"  Annualized:     {m['annualized_return']:+.1%}")
        print(f"  Max drawdown:   {m['max_drawdown']:.1%}")
        print(f"  Sharpe ratio:   {m['sharpe_ratio']:.2f}")
        print(f"{'='*55}\n")

        if self.trades:
            by_pnl = sorted(self.trades, key=lambda t: t.pnl or 0)
            best = by_pnl[-1]
            worst = by_pnl[0]
            print(f"  Best:  {best.symbol} ({best.signal_type}) "
                  f"${best.pnl:+,.0f} entered {best.entry_date}")
            print(f"  Worst: {worst.symbol} ({worst.signal_type}) "
                  f"${worst.pnl:+,.0f} entered {worst.entry_date}")
            print()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Run a historical backtest for the technical primary strategy."""

    def __init__(self, config: Optional[AppConfig] = None):
        self._cfg = config or load_config()

    # ------------------------------------------------------------------ public

    def run(
        self,
        symbols: List[str],
        start: str,
        end: str,
        initial_equity: float = 100_000.0,
        gap_threshold: Optional[float] = None,
        trail_pct: Optional[float] = None,
        slippage_bps: float = 5.0,
        commission_per_share: float = 0.005,
        min_commission: float = 1.0,
        regime_filter: bool = False,
        regime_symbol: str = "SPY",
        regime_symbol_ca: str = "^GSPTSE",
        regime_sma_period: int = 50,
        regime_sma_periods: Optional[List[int]] = None,
        regime_adaptive: bool = False,
        regime_slope_lookback: int = 20,
    ) -> BacktestResult:
        """Run backtest and return result with full trade log + equity curve.

        Args:
            gap_threshold: Override SmartMoneyConfig.gap_up_threshold (default 0.03).
                           Set to 0.02 to fire on smaller gaps.
            trail_pct: Override TraderConfig.default_trail_pct (default 0.05).
                       Wider trail (0.07–0.10) lets winners run longer.
            slippage_bps: Adverse price slip (bps) on entries and market-type
                       exits (stop/trail). Default 5. Set 0 for a gross,
                       frictionless run.
            commission_per_share / min_commission: IBKR-style per-order
                       commission (default $0.005/share, $1 min, capped at 1%
                       of notional), charged on entry and exit.
            regime_filter: When True, block new entries for a symbol on days when
                           its country's regime proxy closes below its
                           `regime_sma_period`-day SMA. Existing positions still
                           exit normally. Gating is per-country, per-symbol.
            regime_symbol: Regime proxy for US symbols (default "SPY").
            regime_symbol_ca: Regime proxy for Canadian symbols — those with a
                           .TO / .V suffix (default "^GSPTSE").
            regime_sma_period: SMA period for the regime check (default 50).
            regime_sma_periods: Optional list of SMA periods; when given,
                           entries require the proxy above ALL of them (e.g.
                           [50, 200]). Overrides regime_sma_period.
            regime_adaptive: When True, use the 3-state recovery-aware detector
                           (bull / recovery / bear) instead of fixed SMA gates.
                           Allowed = above 50-SMA AND (above 200-SMA OR the
                           200-SMA is rising). Captures early recoveries while
                           still blocking bear-market rallies. Overrides the
                           SMA-period gates.
            regime_slope_lookback: Trading-day lookback for the 200-SMA slope
                           used by the adaptive detector (default 20 ≈ 1 month).
        """
        # Build a cloned config with any overrides so we never mutate the global.
        cfg = self._cfg
        if trail_pct is not None or gap_threshold is not None:
            cfg = copy.deepcopy(self._cfg)
            if trail_pct is not None:
                cfg.trader.default_trail_pct = trail_pct
            if gap_threshold is not None:
                cfg.smart_money.gap_up_threshold = gap_threshold

        sm = cfg.smart_money

        # Fetch with warmup: need 200+ trading days before start for SMA
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        fetch_start = (start_dt - timedelta(days=300)).strftime("%Y-%m-%d")
        # The regime proxy needs more history than the strategy: a 200-SMA plus
        # a slope lookback. Fetch it from further back so the gate is valid on
        # day one — without changing the strategy's own indicator warmup.
        regime_fetch_start = (start_dt - timedelta(days=460)).strftime("%Y-%m-%d")

        logger.info(f"Fetching OHLCV for {len(symbols)} symbol(s) ({fetch_start} → {end})")
        raw_bars = self._fetch_all(symbols, fetch_start, end)

        # Precompute indicators for each symbol
        indicators: Dict[str, pd.DataFrame] = {}
        for sym, df in raw_bars.items():
            if len(df) < sm.trend_sma_period + 5:
                logger.warning(f"  {sym}: only {len(df)} bars — need {sm.trend_sma_period+5}, skipping")
                continue
            indicators[sym] = self._precompute(df, sm)

        if not indicators:
            raise ValueError("No symbols had sufficient OHLCV history to run the backtest.")

        # Optional per-country market regime filter.
        # Map each symbol to its country's proxy, then fetch each unique proxy once.
        regime_maps: Dict[str, Dict[str, bool]] = {}
        symbol_proxy: Dict[str, str] = {}
        sma_periods = regime_sma_periods or [regime_sma_period]
        regime_label = (
            f"adaptive(50/200, slope {regime_slope_lookback}d)"
            if regime_adaptive
            else "+".join(str(p) for p in sma_periods)
        )
        if regime_filter:
            for sym in indicators:
                symbol_proxy[sym] = regime_symbol_ca if _is_canadian(sym) else regime_symbol
            for proxy in sorted(set(symbol_proxy.values())):
                logger.info(f"Building regime filter from {proxy} [{regime_label}]")
                if regime_adaptive:
                    regime_maps[proxy] = self._fetch_regime_adaptive(
                        proxy, regime_fetch_start, end, regime_slope_lookback
                    )
                else:
                    regime_maps[proxy] = self._fetch_regime(
                        proxy, regime_fetch_start, end, sma_periods
                    )

        def entries_allowed_for(sym: str, date_str: str) -> bool:
            """Per-symbol regime gate. Fail-open if filter off or proxy missing."""
            if not regime_filter:
                return True
            proxy = symbol_proxy.get(sym, regime_symbol)
            return regime_maps.get(proxy, {}).get(date_str, True)

        # Build sorted list of trading dates within [start, end]
        all_dates: set = set()
        for df in indicators.values():
            mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
            all_dates.update(df.loc[mask].index.strftime("%Y-%m-%d"))
        trading_dates = sorted(all_dates)

        if not trading_dates:
            raise ValueError(f"No trading dates found between {start} and {end}.")

        portfolio = SimulatedPortfolio(
            initial_equity, cfg,
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            min_commission=min_commission,
        )
        equity_curve: List[float] = []
        pending_pullback: Dict[str, Tuple[str, float]] = {}  # sym → (signal_type, confidence)

        logger.info(f"Running simulation over {len(trading_dates)} trading days "
                    f"with {len(indicators)} symbol(s)")

        for date in trading_dates:
            ts = pd.Timestamp(date)

            # -- Step 1: fill pending pullback entries at today's open ----------
            # Pending entries were queued before we knew the regime; honour them
            # only if the symbol's country regime still allows entries today.
            for sym, (sig_type, confidence) in list(pending_pullback.items()):
                if not entries_allowed_for(sym, date):
                    continue
                df = indicators.get(sym)
                if df is not None and ts in df.index:
                    fill = float(df.loc[ts, "open"])
                    if fill > 0:
                        portfolio.open_position(sym, date, fill, sig_type, confidence)
            pending_pullback = {}

            # -- Step 2: gap up entries at today's open -------------------------
            if sm.gap_up_enabled:
                for sym, df in indicators.items():
                    if ts not in df.index:
                        continue
                    if sym in portfolio.open_symbols:
                        continue
                    if not entries_allowed_for(sym, date):
                        continue
                    row = df.loc[ts]
                    if _is_true(row.get("is_gap_up")):
                        fill = float(row["open"])
                        portfolio.open_position(sym, date, fill, "gap_up", 0.65)

            # -- Step 3: simulate exits using today's high/low/close ------------
            day_bars: Dict[str, pd.Series] = {}
            for sym in list(portfolio.open_symbols):
                df = indicators.get(sym)
                if df is not None and ts in df.index:
                    day_bars[sym] = df.loc[ts]
            portfolio.update_exits(date, day_bars)

            # -- Step 4: queue pullback signals for tomorrow's fill --------------
            if sm.ema_pullback_enabled:
                for sym, df in indicators.items():
                    if ts not in df.index:
                        continue
                    if sym in portfolio.open_symbols:
                        continue
                    if sym in pending_pullback:
                        continue
                    if not entries_allowed_for(sym, date):
                        continue
                    row = df.loc[ts]
                    if _is_true(row.get("is_pullback")):
                        pending_pullback[sym] = ("ema_pullback", 0.50)

            # -- Step 5: record end-of-day equity --------------------------------
            eod_prices = {
                sym: float(df.loc[ts, "close"])
                for sym, df in indicators.items()
                if ts in df.index
            }
            equity_curve.append(portfolio.record_equity(eod_prices))

        # Close any remaining positions at last day's close
        last_ts = pd.Timestamp(trading_dates[-1])
        last_prices = {
            sym: float(df.loc[last_ts, "close"])
            for sym, df in indicators.items()
            if last_ts in df.index
        }
        portfolio.close_all(trading_dates[-1], last_prices)

        return BacktestResult(
            trades=portfolio.trade_log(),
            equity_curve=equity_curve,
            start=start,
            end=end,
            initial_equity=initial_equity,
            symbols=sorted(indicators.keys()),
            regime_filter=regime_filter,
            regime_symbol=regime_symbol,
            regime_symbol_ca=regime_symbol_ca,
            regime_sma_label=regime_label,
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
        )

    # ----------------------------------------------------------------- private

    def _fetch_all(self, symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV for each symbol directly from yfinance."""
        try:
            import yfinance as yf
        except ImportError:
            raise RuntimeError("yfinance is required for backtesting: pip install yfinance")

        result: Dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                df = ticker.history(start=start, end=end, auto_adjust=True)
                if df.empty:
                    logger.warning(f"  {sym}: yfinance returned empty — skipping")
                    continue

                df.columns = [c.lower() for c in df.columns]
                keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
                df = df[keep].copy()
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                df.index = pd.DatetimeIndex(pd.to_datetime(df.index.date))
                df = df[~df.index.duplicated(keep="first")]
                df.sort_index(inplace=True)

                result[sym.upper()] = df
                logger.debug(f"  {sym}: {len(df)} bars fetched")
            except Exception as e:
                logger.warning(f"  {sym}: fetch failed — {e}")

        return result

    def _fetch_regime(
        self,
        symbol: str,
        fetch_start: str,
        end: str,
        sma_periods: List[int],
    ) -> Dict[str, bool]:
        """Return {date_str: regime_ok} for the regime symbol.

        regime_ok is True only when the close is above EVERY SMA in
        `sma_periods`. With a single period this is the simple "above the
        N-SMA" gate; with [50, 200] it requires the fast trend AND the slow
        trend to agree (catches a deteriorating tape on the fast average while
        demanding the slow average confirm before re-entering).

        Dates absent from the result are treated as regime-OK (fail-open).
        """
        bars = self._fetch_all([symbol], fetch_start, end)
        df = bars.get(symbol.upper())
        if df is None or df.empty:
            logger.warning(f"  regime: could not fetch {symbol} — filter disabled (fail-open)")
            return {}

        df = df.copy()
        above_all = pd.Series(True, index=df.index)
        for period in sma_periods:
            sma = df["close"].rolling(period).mean()
            # close > NaN is False, so warmup rows (before the longest SMA is
            # defined) resolve to regime-off — harmless, they precede `start`.
            above_all = above_all & (df["close"] > sma)

        result: Dict[str, bool] = {}
        for ts, val in above_all.items():
            result[ts.strftime("%Y-%m-%d")] = bool(val)
        return result

    def _fetch_regime_adaptive(
        self,
        symbol: str,
        fetch_start: str,
        end: str,
        slope_lookback: int,
    ) -> Dict[str, bool]:
        """Recovery-aware 3-state regime gate keyed on the 200-SMA slope.

        State per day:
          - BULL     : close > 200-SMA                      → entries allowed
          - RECOVERY : close < 200-SMA but 200-SMA rising
                       AND close > 50-SMA                   → entries allowed
          - BEAR     : close < 200-SMA and 200-SMA falling  → entries blocked
          (close < 50-SMA is always blocked)

        allowed = (close > 50-SMA) AND (close > 200-SMA OR 200-SMA rising)

        The 200-SMA slope is what separates a true recovery (slow average
        turning up) from a bear-market rally (slow average still falling).
        Dates absent from the result are treated as regime-OK (fail-open).
        """
        bars = self._fetch_all([symbol], fetch_start, end)
        df = bars.get(symbol.upper())
        if df is None or df.empty:
            logger.warning(f"  regime: could not fetch {symbol} — filter disabled (fail-open)")
            return {}

        df = df.copy()
        sma50 = df["close"].rolling(50).mean()
        sma200 = df["close"].rolling(200).mean()
        sma200_rising = (sma200 - sma200.shift(slope_lookback)) >= 0

        above50 = df["close"] > sma50
        above200 = df["close"] > sma200
        allowed = above50 & (above200 | sma200_rising)

        # Classify for a one-line breakdown (helps confirm the detector works).
        bull = above200
        recovery = (~above200) & sma200_rising & above50
        bear = (~above200) & (~sma200_rising)

        result: Dict[str, bool] = {}
        n_bull = n_rec = n_bear = 0
        for ts, val in allowed.items():
            result[ts.strftime("%Y-%m-%d")] = bool(val)
            if bool(bull.get(ts, False)):
                n_bull += 1
            elif bool(recovery.get(ts, False)):
                n_rec += 1
            elif bool(bear.get(ts, False)):
                n_bear += 1
        total = max(1, n_bull + n_rec + n_bear)
        logger.info(
            f"  {symbol} adaptive regime mix: "
            f"bull {n_bull/total:.0%} / recovery {n_rec/total:.0%} / bear {n_bear/total:.0%}"
        )
        return result

    @staticmethod
    def _precompute(
        df: pd.DataFrame,
        sm_cfg,
    ) -> pd.DataFrame:
        """Add indicator columns to the bar DataFrame.

        Uses shift(1) for previous-day values everywhere so that
        no same-day information leaks into signal generation.
        """
        df = df.copy()

        sma_p = sm_cfg.trend_sma_period   # 200
        ema_p = sm_cfg.ema_period          # 8
        ema_tol = sm_cfg.ema_tolerance     # 0.02
        gap_thr = sm_cfg.gap_up_threshold  # configurable (default 0.03, CLI can override)

        df["sma_200"] = df["close"].rolling(sma_p).mean()
        df["ema_8"] = df["close"].ewm(span=ema_p, adjust=False).mean()
        df["above_sma"] = df["close"] > df["sma_200"]

        prev_close = df["close"].shift(1)
        prev_ema8 = df["ema_8"].shift(1)

        # Gap up: open clears previous close by gap_thr
        df["is_gap_up"] = (df["open"] > prev_close * (1.0 + gap_thr)) & df["above_sma"]

        # EMA pullback: today's open at or below yesterday's 8 EMA (+ tolerance)
        df["is_pullback"] = (
            (df["open"] <= prev_ema8 * (1.0 + ema_tol))
            & (df["open"] > 0)
            & df["above_sma"]
        )

        return df


def _is_true(val) -> bool:
    """Safe boolean check for pandas scalar that may be NaN."""
    try:
        return bool(val) and not (val != val)  # NaN check via self-inequality
    except (TypeError, ValueError):
        return False


def _is_canadian(symbol: str) -> bool:
    """True for TSX / TSX-V tickers (yfinance .TO / .V suffix)."""
    s = symbol.upper()
    return s.endswith(".TO") or s.endswith(".V")
