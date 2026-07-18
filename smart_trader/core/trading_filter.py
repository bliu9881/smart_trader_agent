"""TradingFilter — portfolio-aware signal generation.

Implements the primitives for the three-path signal flow described in
Requirement 5:

  Path A: Entry_Calculator trigger — generates a LONG Signal when a Top_N_Set
          stock's current price ≤ its Optimal_Entry_Price and the bot doesn't
          already hold it.
  Path B: SmartMoneyScanner filter — drops scanner candidates whose symbol
          isn't in the Top_N_Set.
  Path C: Fallback — invoked by main.py when no portfolio snapshot exists or
          portfolio_filter_enabled=False. This module doesn't implement C;
          main.py skips the filter entirely in that case.

The filter only generates BUY signals; exits are RiskManager's responsibility.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, TYPE_CHECKING

from smart_trader.core.entry_calculator import TechnicalSignals
from smart_trader.core.portfolio_scorer import ScoredStock
from smart_trader.core.signal import Signal
from smart_trader.core.smart_money import CandidateSymbol
from smart_trader.settings.config import SmartMoneyConfig, TraderConfig

if TYPE_CHECKING:
    from smart_trader.core.catalyst_analyzer import CatalystEvent

logger = logging.getLogger(__name__)


@dataclass
class TopNStock:
    """Minimal view of a top-N portfolio stock needed by the filter."""
    symbol: str
    composite_score: float
    optimal_entry_price: Optional[float]
    overlap_count: int
    sources: List[str]
    # Largest single-fund weight (fraction of that fund's book this stock
    # represents). Used by the tradability gate to keep concentrated
    # single-fund bets like AAPL at 22.6% of Berkshire in scope. Default 0.0
    # preserves the constructor signature for existing tests.
    max_single_fund_weight: float = 0.0


@dataclass
class FilterOutcome:
    """Per-candidate bookkeeping (dropped / passed, with reason)."""
    symbol: str
    kept: bool
    reason: str  # "passed", "not-in-top-n", "not-in-portfolio"


def is_tradable(
    stock: "TopNStock",
    min_overlap_count: int,
    concentration_exception: float,
) -> bool:
    """Tradability gate for top-N stocks (Req 5.7).

    A top-N stock may trigger Path A / Path B / Overlap only if EITHER:
      (a) it's held by at least `min_overlap_count` distinct funds, OR
      (b) a single fund holds it at >= `concentration_exception` of their book.

    Rule (b) exists so legitimate concentrated single-fund bets (e.g. AAPL at
    22.6% of Berkshire) aren't blocked by the overlap rule; low-conviction
    picks like ESLT at 1.6% of ARK are.
    """
    if stock.overlap_count >= min_overlap_count:
        return True
    if stock.max_single_fund_weight >= concentration_exception:
        return True
    return False


def _catalyst_confidence_delta(
    events: List["CatalystEvent"],
    cfg: SmartMoneyConfig,
    is_gap_up: bool,
) -> tuple:
    """Return (confidence_delta, top_event) for a symbol's catalyst event list.

    Picks the highest-magnitude event and maps it to the appropriate config
    adjustment based on signal type (gap up vs EMA pullback).
    Empty `events` means no catalyst found — applies a ghost-gap penalty for
    gap ups, no penalty for pullbacks (technical retracements don't need news).
    """
    if not events:
        delta = -cfg.catalyst_gap_penalty_no_news if is_gap_up else 0.0
        return delta, None

    # Pick most impactful event (highest absolute sentiment)
    top = max(events, key=lambda e: abs(e.sentiment))

    STRONG_POSITIVE = {"earnings_beat", "guidance_raise", "acquisition"}
    MODERATE_POSITIVE = {"analyst_upgrade", "product_launch", "partnership", "buyback"}
    NEGATIVE = {"earnings_miss", "guidance_cut", "downgrade", "lawsuit", "recall"}

    if top.catalyst_type in STRONG_POSITIVE and top.sentiment > 0:
        delta = cfg.catalyst_gap_boost_strong if is_gap_up else cfg.catalyst_pullback_boost_strong
    elif top.catalyst_type in MODERATE_POSITIVE and top.sentiment > 0:
        delta = cfg.catalyst_gap_boost_moderate if is_gap_up else cfg.catalyst_pullback_boost_moderate
    elif top.catalyst_type in NEGATIVE or top.sentiment < 0:
        delta = -(cfg.catalyst_gap_penalty_negative if is_gap_up else cfg.catalyst_pullback_penalty_negative)
    else:
        # "other" category with neutral sentiment
        delta = 0.0

    return delta, top


class TradingFilter:
    """Stateless per-cycle filter; main.py constructs a fresh instance each
    cycle with the latest Top_N_Set and optimal entry prices."""

    def __init__(
        self,
        smart_money_config: SmartMoneyConfig,
        trader_config: TraderConfig,
    ):
        self.sm_config = smart_money_config
        self.trader_config = trader_config

    # ---------------------------------------------------------------- Path A
    def generate_entry_signals(
        self,
        top_n: List[TopNStock],
        held_symbols: Set[str],
        current_prices: Dict[str, float],
    ) -> List[Signal]:
        """Emit LONG Signals for top-N stocks whose current price has reached
        the Optimal_Entry_Price and that aren't already held.

        current_prices maps symbol -> latest close. Stocks missing a price or
        a valid entry level are skipped (logged at debug).
        """
        if not self.sm_config.entry_price_bypass_enabled:
            logger.debug("  trading_filter: entry price bypass disabled")
            return []

        tolerance = max(0.0, float(self.sm_config.entry_price_tolerance))

        signals: List[Signal] = []
        for stock in top_n:
            if stock.symbol in held_symbols:
                continue
            entry = stock.optimal_entry_price
            if entry is None or entry <= 0:
                continue
            price = current_prices.get(stock.symbol)
            if price is None or price <= 0:
                continue
            # Path A fires when price is at or near the optimal entry. A
            # `tolerance` of 0.05 means "up to 5% above entry still triggers."
            cap = entry * (1.0 + tolerance)
            if price > cap:
                continue

            # Price is at or below the optimal entry — generate signal
            stop_pct = self.trader_config.default_stop_pct
            take_pct = self.trader_config.default_take_profit_pct
            trail_pct = self.trader_config.default_trail_pct

            signals.append(Signal(
                symbol=stock.symbol,
                direction="LONG",
                confidence=min(1.0, stock.composite_score),
                entry_price=price,
                stop_loss=price * (1 - stop_pct),
                take_profit=price * (1 + take_pct),
                trailing_stop_pct=trail_pct,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=datetime.now(),
                reasoning=(
                    f"SmartMoneyPortfolio entry: price ${price:.2f} <= optimal "
                    f"${entry:.2f} (composite {stock.composite_score:.3f}, "
                    f"{stock.overlap_count} funds)"
                ),
                strategy_name="SmartMoneyPortfolio_Entry",
                metadata={
                    "smart_money_portfolio": True,
                    "composite_score": stock.composite_score,
                    "optimal_entry_price": entry,
                    "overlap_count": stock.overlap_count,
                    "sources": stock.sources,
                },
            ))
            logger.info(
                f"  trading_filter: entry trigger {stock.symbol} — "
                f"price ${price:.2f} within tolerance band "
                f"(entry ${entry:.2f}, cap ${cap:.2f}, tolerance {tolerance*100:.1f}%)"
            )
        return signals

    # ---------------------------------------------------------------- Overlap
    def generate_overlap_signals(
        self,
        top_n: List[TopNStock],
        scanner_candidates: Iterable[CandidateSymbol],
        held_symbols: Set[str],
        current_prices: Dict[str, float],
        min_conviction_score: float,
    ) -> List[Signal]:
        """Emit boosted LONG signals for symbols where both Path A and Path B trigger.

        Conditions per symbol:
          - Appears in `top_n` with an optimal_entry_price
          - Not in `held_symbols`
          - `current_prices[sym]` is available and ≤ optimal_entry_price (Path A)
          - Appears in `scanner_candidates` with conviction_score ≥ min_conviction_score (Path B)

        The returned Signal carries:
          - `strategy_name = "SmartMoneyPortfolio_Overlap"`
          - `confidence = min(1.0, 2 × base)`
          - `metadata.overlap_boost = True`
          - `metadata.risk_multiplier = 2.0`  (main.py passes this as
             risk_pct_override to RiskManager.compute_position_size)
          - `metadata.scanner_sources` with the providers that flagged it

        Callers should suppress the per-symbol Path A entry signal AND the
        per-symbol Path B scanner signal for each overlap symbol to avoid
        triple-entry for the same stock.
        """
        if not self.sm_config.entry_price_bypass_enabled:
            return []

        tolerance = max(0.0, float(self.sm_config.entry_price_tolerance))

        scanner_by_sym: Dict[str, CandidateSymbol] = {}
        for c in scanner_candidates:
            if c.conviction_score >= min_conviction_score:
                scanner_by_sym[c.symbol] = c

        signals: List[Signal] = []
        for stock in top_n:
            sym = stock.symbol
            if sym in held_symbols:
                continue
            if sym not in scanner_by_sym:
                continue
            entry = stock.optimal_entry_price
            if entry is None or entry <= 0:
                continue
            price = current_prices.get(sym)
            if price is None or price <= 0:
                continue
            # Overlap signal inherits Path A's tolerance (both are entry-price
            # triggered); Path B alone has no price gate.
            cap = entry * (1.0 + tolerance)
            if price > cap:
                continue

            scan = scanner_by_sym[sym]
            stop_pct = self.trader_config.default_stop_pct
            take_pct = self.trader_config.default_take_profit_pct
            trail_pct = self.trader_config.default_trail_pct
            base_confidence = min(1.0, stock.composite_score)
            boosted = min(1.0, base_confidence * 2.0)

            signals.append(Signal(
                symbol=sym,
                direction="LONG",
                confidence=boosted,
                entry_price=price,
                stop_loss=price * (1 - stop_pct),
                take_profit=price * (1 + take_pct),
                trailing_stop_pct=trail_pct,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=datetime.now(),
                reasoning=(
                    f"OVERLAP 2x: price ${price:.2f} within tolerance of entry ${entry:.2f} "
                    f"(cap ${cap:.2f}) AND scanner conviction {scan.conviction_score:.2f} "
                    f"from {len(scan.sources)} source(s) [{', '.join(scan.sources)}]"
                ),
                strategy_name="SmartMoneyPortfolio_Overlap",
                metadata={
                    "smart_money_portfolio": True,
                    "overlap_boost": True,
                    "risk_multiplier": 2.0,
                    "composite_score": stock.composite_score,
                    "conviction_score": scan.conviction_score,
                    "optimal_entry_price": entry,
                    "overlap_count": stock.overlap_count,
                    "sources": stock.sources,
                    "scanner_sources": scan.sources,
                },
            ))
            logger.info(
                f"  trading_filter: OVERLAP {sym} — price ${price:.2f} within tolerance "
                f"(entry ${entry:.2f}, cap ${cap:.2f}) AND scanner conviction "
                f"{scan.conviction_score:.2f} from {scan.sources}"
            )
        return signals

    # -------------------------------------------------------- Technical primary
    def generate_technical_primary_signals(
        self,
        tech_signals: Dict[str, TechnicalSignals],
        scored_stocks: Dict[str, ScoredStock],
        scanner_candidates: Dict[str, CandidateSymbol],
        held_symbols: Set[str],
        catalyst_events: Optional[Dict[str, List["CatalystEvent"]]] = None,
    ) -> List[Signal]:
        """Humbled Trader primary path: 200 SMA trend filter + 8 EMA pullback / gap up.

        Smart money data (scored_stocks, scanner_candidates) is used only as a
        confidence scoring bonus — it does NOT gate entry; technical signal quality
        determines whether to enter, and smart money backing determines how much.

        Optional catalyst_events enriches confidence further:
          - Strong positive news (earnings beat, acquisition): +0.15–0.20
          - Gap up with no news: -0.05 (ghost-gap penalty)
          - Negative news: -0.15–0.20

        Emits one Signal per qualifying symbol with:
          - strategy_name: "Technical_EMAPullback" or "Technical_GapUp"
          - confidence: base (0.50/0.65) + smart money bonus + catalyst delta, capped at 1.0
          - metadata["risk_multiplier"]: 0.75 / 1.0 / 1.5 based on confidence tier
        """
        cfg = self.sm_config
        signals: List[Signal] = []

        for sym, ts in tech_signals.items():
            if ts.signal_type == "none":
                continue
            if sym in held_symbols:
                continue
            if cfg.trend_filter_enabled and not ts.above_200_sma:
                logger.debug(f"  trading_filter technical: {sym} below 200 SMA — skipping")
                continue
            if ts.current_price is None or ts.current_price <= 0:
                continue

            # Base confidence by signal type
            if ts.signal_type in ("gap_up", "both"):
                base_confidence = 0.65
                strategy_name = "Technical_GapUp"
            else:
                base_confidence = 0.50
                strategy_name = "Technical_EMAPullback"

            # Smart money bonus (additive)
            bonus = 0.0
            overlap_count = 0
            conviction_score = 0.0

            scored = scored_stocks.get(sym)
            if scored is not None:
                overlap_count = scored.overlap_count
                if overlap_count >= 2:
                    bonus += cfg.sm_bonus_overlap_multi
                elif overlap_count == 1:
                    bonus += cfg.sm_bonus_overlap_single

            candidate = scanner_candidates.get(sym)
            if candidate is not None:
                conviction_score = candidate.conviction_score
                if conviction_score >= cfg.sm_bonus_high_conviction:
                    bonus += 0.25
                elif conviction_score >= cfg.sm_bonus_low_conviction:
                    bonus += 0.15

            # Catalyst adjustment (applied to both gap up and EMA pullback)
            catalyst_adj = 0.0
            top_catalyst = None
            catalyst_event_list: List = []
            if catalyst_events is not None:
                events = catalyst_events.get(sym, [])
                catalyst_event_list = events
                catalyst_adj, top_catalyst = _catalyst_confidence_delta(
                    events, cfg, is_gap_up=ts.is_gap_up
                )

            confidence = min(1.0, max(0.0, base_confidence + bonus + catalyst_adj))

            # Risk multiplier tier
            if confidence >= cfg.sm_risk_thresh_high:
                risk_mult = cfg.sm_risk_mult_high
            elif confidence >= cfg.sm_risk_thresh_low:
                risk_mult = cfg.sm_risk_mult_mid
            else:
                risk_mult = cfg.sm_risk_mult_low

            price = ts.current_price
            stop_pct = self.trader_config.default_stop_pct
            take_pct = self.trader_config.default_take_profit_pct
            trail_pct = self.trader_config.default_trail_pct

            ema_str = f"{ts.ema_8:.2f}" if ts.ema_8 is not None else "n/a"
            catalyst_note = (
                f", catalyst={top_catalyst.catalyst_type}({catalyst_adj:+.2f})"
                if top_catalyst else (
                    f", no_catalyst({catalyst_adj:+.2f})" if catalyst_adj != 0.0 else ""
                )
            )

            metadata: Dict[str, Any] = {
                "technical_primary": True,
                "signal_type": ts.signal_type,
                "above_200_sma": ts.above_200_sma,
                "ema_8": ts.ema_8,
                "smart_money_bonus": bonus,
                "catalyst_adjustment": catalyst_adj,
                "overlap_count": overlap_count,
                "conviction_score": conviction_score,
                "risk_multiplier": risk_mult,
            }
            if catalyst_event_list:
                metadata["catalyst_events"] = [asdict(e) for e in catalyst_event_list]
                metadata["catalyst_events_count"] = len(catalyst_event_list)
            if top_catalyst is not None:
                metadata["top_catalyst"] = top_catalyst.headline
                metadata["top_catalyst_type"] = top_catalyst.catalyst_type
                metadata["top_catalyst_sentiment"] = top_catalyst.sentiment

            signals.append(Signal(
                symbol=sym,
                direction="LONG",
                confidence=confidence,
                entry_price=price,
                stop_loss=price * (1.0 - stop_pct),
                take_profit=price * (1.0 + take_pct),
                trailing_stop_pct=trail_pct,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=datetime.now(),
                reasoning=(
                    f"{strategy_name}: price ${price:.2f}, "
                    f"ema_8={ema_str}, "
                    f"above_200_sma={ts.above_200_sma}, "
                    f"conf={confidence:.2f} (base={base_confidence:.2f} + sm={bonus:.2f}"
                    f"{catalyst_note}), "
                    f"overlap={overlap_count}, conviction={conviction_score:.2f}, "
                    f"risk_mult={risk_mult:.2f}"
                ),
                strategy_name=strategy_name,
                metadata=metadata,
            ))
            logger.info(
                f"  trading_filter technical: {strategy_name} {sym} — "
                f"conf={confidence:.2f} (base={base_confidence:.2f}+sm={bonus:.2f}"
                f"{catalyst_note}), "
                f"overlap={overlap_count}, conviction={conviction_score:.2f}, "
                f"risk_mult={risk_mult:.2f}"
            )

        return signals

    # ---------------------------------------------------------------- Path B
    def filter_scanner_candidates(
        self,
        candidates: Iterable[CandidateSymbol],
        portfolio_symbols: Set[str],
        top_n_symbols: Set[str],
    ) -> List[FilterOutcome]:
        """Return one FilterOutcome per candidate explaining the gate decision.

        Callers keep `kept=True` outcomes as pass-through candidates; `kept=False`
        outcomes are logged-only for the UI/audit trail.
        """
        outcomes: List[FilterOutcome] = []
        for c in candidates:
            if c.symbol in top_n_symbols:
                outcomes.append(FilterOutcome(symbol=c.symbol, kept=True, reason="passed"))
            elif c.symbol in portfolio_symbols:
                outcomes.append(FilterOutcome(symbol=c.symbol, kept=False, reason="not-in-top-n"))
            else:
                outcomes.append(FilterOutcome(symbol=c.symbol, kept=False, reason="not-in-portfolio"))
        return outcomes
