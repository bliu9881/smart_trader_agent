"""
Smart Trader — main orchestrator.

  python3 -m smart_trader.main              → live paper-trading loop
  python3 -m smart_trader.main dry-run      → full loop, no orders placed

Startup sequence:
  load config → connect IBKR → verify paper account → init risk manager →
  init smart-money scanner → init ladder-in engine → start API server →
  run 5-min cycle forever.

Each cycle:
  1. Fetch portfolio state (equity, cash, positions)
  2. Ask SmartMoneyScanner for ranked candidates
  3. Build LONG signals for top candidates not already held
  4. Evaluate held positions for ladder-in dip-buy signals
  5. RiskManager validates every signal (circuit breakers, sector caps, etc.)
  6. OrderExecutor submits approved signals as bracket + trailing stop
  7. Push state to API → React dashboard renders
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

from smart_trader import demo_data
from smart_trader.api.state import store as api_state
from smart_trader.core.catalyst_analyzer import CatalystAnalyzer
from smart_trader.core.entry_calculator import EntryCalculator
from smart_trader.core.holdings_scraper import HoldingsScraper
from smart_trader.core.ladder_in import LadderInEngine
from smart_trader.core.portfolio_scorer import PortfolioScorer, ScoredStock
from smart_trader.core.regime import classify_regime
from smart_trader.core.risk_manager import PortfolioState, RiskManager
from smart_trader.core.sector_resolver import SectorResolver
from smart_trader.core.signal import Signal
from smart_trader.core.smart_money import SmartMoneyScanner
from smart_trader.core.trading_filter import TopNStock, TradingFilter, is_tradable
from smart_trader.data.ohlcv_store import OHLCVStore
from smart_trader.data.portfolio_store import PortfolioStore
from smart_trader.monitoring.alerts import AlertManager
from smart_trader.monitoring.logger import setup_logging
from smart_trader.settings.config import AppConfig, load_config
from smart_trader.settings.credentials import load_credentials

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_broker_components():
    """Import broker modules lazily to avoid ib_insync event-loop issues."""
    from smart_trader.broker.ibkr_client import IBKRClient
    from smart_trader.broker.market_data import MarketDataFetcher
    from smart_trader.broker.order_executor import OrderExecutor
    from smart_trader.broker.position_tracker import PositionTracker
    return IBKRClient, OrderExecutor, PositionTracker, MarketDataFetcher


def _save_snapshot(config: AppConfig, state: Dict[str, Any]) -> None:
    path = Path(config.monitoring.state_snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str))
    logger.info(f"Saved snapshot to {path}")


def _load_snapshot(config: AppConfig) -> Optional[Dict[str, Any]]:
    path = Path(config.monitoring.state_snapshot_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"Failed to load snapshot: {e}")
        return None


# ---------------------------------------------------------------------------
# SmartTrader orchestrator
# ---------------------------------------------------------------------------

class SmartTrader:
    """Main trading orchestrator."""

    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self._running = False

        self.client = None
        self.executor = None
        self.positions_tracker = None
        self.market_data = None
        self.risk: Optional[RiskManager] = None
        self.ladder_engine: Optional[LadderInEngine] = None
        self.scanner: Optional[SmartMoneyScanner] = None
        self.alerts = AlertManager(config.monitoring)
        self._previous_position_symbols: set = set()
        # Position open times (for min-holding-period guard on signal-driven
        # exits) and recent-exit timestamps (for re-entry cooldown). Both are
        # in-memory: lost on restart, which means the guards become permissive
        # for one cycle after restart. Acceptable for Phase 2; persist to
        # SQLite if false-positive whipsaws show up in paper trading.
        self._position_open_ts: Dict[str, datetime] = {}
        self._recent_exits: Dict[str, datetime] = {}
        # Last datetime each held symbol was observed in top-N. Symbols
        # never observed (manual holds, pre-bot positions) are absent and
        # the drop-out trigger silently skips them. Reset on restart, so
        # post-restart there is no drop-out signal until the symbol is
        # observed in at least one valid top-N refresh.
        self._held_top_n_history: Dict[str, datetime] = {}
        # Datetime each eligible held symbol first dropped below the
        # min_held_conviction_score floor. Eligibility = previously observed
        # in top-N. Cleared when score recovers, when symbol is no longer
        # held, or when scanner data is unavailable for the cycle.
        self._held_conviction_below_since: Dict[str, datetime] = {}

        # Smart_Money_Portfolio pipeline (optional — disabled if init fails)
        self.sector_resolver: Optional[SectorResolver] = None
        self.ohlcv_store: Optional[OHLCVStore] = None
        self.holdings_scraper: Optional[HoldingsScraper] = None
        self.portfolio_scorer: Optional[PortfolioScorer] = None
        self.entry_calculator: Optional[EntryCalculator] = None
        self.portfolio_store: Optional[PortfolioStore] = None
        self.trading_filter: Optional[TradingFilter] = None
        self._latest_scored: List[ScoredStock] = []
        self._latest_entry_prices: Dict[str, Optional[float]] = {}
        self._last_portfolio_refresh_date: Optional[str] = None
        # Market-regime gate — recomputed once per ET trading day (daily bars
        # don't change intraday). _regime_state is a RegimeState or None.
        self._regime_state = None
        self._regime_date: Optional[str] = None

        # Qwen agent components (optional — None when disabled or init fails)
        self.signal_arbitrator: Optional["SignalArbitrator"] = None
        self.commentary_generator: Optional["CommentaryGenerator"] = None
        # Concrete reasons captured when a component fails to initialize, so the
        # dashboard "failed" status is diagnosable instead of opaque. None means
        # "no init error" (component is either wired or intentionally disabled).
        self._catalyst_init_error: Optional[str] = None
        self._qwen_init_error: Optional[str] = None
        self._cycle_count: int = 0

        # Demo seeding: when DEMO_SEED is truthy, fill empty dashboard sections
        # with sample data (real data always wins once the pipeline produces it).
        self._demo_seed: bool = demo_data._is_on(os.environ.get("DEMO_SEED"))

    # --------------------------------------------------------------- startup

    def start(self) -> None:
        setup_logging(self.config.monitoring)
        logger.info("=" * 60)
        logger.info(f"Smart Trader starting (dry_run={self.dry_run})")
        logger.info("=" * 60)

        creds = load_credentials()
        self.config.broker.host = creds["ibkr_host"]
        self.config.broker.port = creds["ibkr_port"]
        self.config.broker.client_id = creds["ibkr_client_id"]

        IBKRClient, OrderExecutor, PositionTracker, MarketDataFetcher = _get_broker_components()

        # --- Attempt IBKR connection with fallback to mock broker ---
        mock_mode = False
        try:
            logger.info("Connecting to IBKR...")
            self.client = IBKRClient(self.config.broker)
            if not self.client.connect():
                raise ConnectionError("IBKRClient.connect() returned False")

            if not self.client.is_paper:
                self._verify_live_opt_in(creds, self.client.account_id)

            equity = self.client.get_portfolio_value()
            logger.info(f"Account: {self.client.account_id} | Equity: ${equity:,.2f}")

            self.executor = OrderExecutor(self.client)
            self.positions_tracker = PositionTracker(self.client)
            self.market_data = MarketDataFetcher(self.client)
        except Exception as e:
            logger.warning(
                f"IBKR connection failed: {e} — switching to mock broker mode"
            )
            mock_mode = True
            self.config.demo_mode = True

            # Apply the 4-provider demo config to minimize network deps
            from smart_trader.settings.config import create_demo_smart_money_config
            self.config.smart_money = create_demo_smart_money_config()

            # Instantiate MockBroker (ohlcv_store set later after init)
            from smart_trader.broker.mock_broker import MockBroker
            mock = MockBroker(ohlcv_store=None)
            self.client = mock
            self.executor = mock
            self.positions_tracker = mock
            self.market_data = mock

            equity = mock.get_portfolio_value()
            logger.info(
                f"Mock broker active | Account: {mock.account_id} | "
                f"Equity: ${equity:,.2f}"
            )

        # Store broker mode in API state
        api_state.update(broker_mode="mock-broker" if mock_mode else "live-broker")

        self.risk = RiskManager(self.config.risk)
        self.risk.initialize(equity)

        self.ladder_engine = LadderInEngine(
            self.config.ladder_in,
            default_stop_pct=self.config.trader.default_stop_pct,
            default_trail_pct=self.config.trader.default_trail_pct,
        )

        try:
            self.scanner = SmartMoneyScanner(self.config.smart_money, self.config.risk)
            logger.info("SmartMoneyScanner initialized")
        except Exception as e:
            logger.warning(f"SmartMoneyScanner init failed (continuing without): {e}")
            self.scanner = None

        # Smart_Money_Portfolio pipeline
        try:
            self.sector_resolver = SectorResolver()
            # Inject cached sectors into risk config so the risk manager sees them
            self.config.risk.sector_map.update(self.sector_resolver.get_map())
            logger.info(f"SectorResolver initialized ({len(self.sector_resolver.get_map())} cached sectors)")
        except Exception as e:
            logger.warning(f"SectorResolver init failed (continuing with empty map): {e}")
            self.sector_resolver = None

        # CatalystAnalyzer is initialized independently so it works even
        # without Supabase credentials (which the portfolio pipeline needs).
        try:
            self.catalyst_analyzer = (
                CatalystAnalyzer(
                    self.config.smart_money,
                    cache_dir=self.config.smart_money.catalyst_cache_dir,
                )
                if self.config.smart_money.catalyst_enabled
                else None
            )
            if self.catalyst_analyzer:
                logger.info("CatalystAnalyzer initialized")
        except Exception as e:
            logger.warning(
                f"CatalystAnalyzer init failed (continuing without): {e}",
                exc_info=True,
            )
            self.catalyst_analyzer = None
            self._catalyst_init_error = f"analyzer init failed: {e}"

        try:
            self.ohlcv_store = OHLCVStore(
                db_path=self.config.data.db_path,
                supabase_sync_enabled=self.config.data.supabase_sync_enabled,
            )
            self.holdings_scraper = HoldingsScraper(self.config.smart_money)
            self.portfolio_scorer = PortfolioScorer(self.config.smart_money, self.ohlcv_store)
            self.entry_calculator = EntryCalculator(self.config.smart_money, self.ohlcv_store)
            self.portfolio_store = PortfolioStore(
                db_path=self.config.smart_money.portfolio_db_path,
                retention_days=self.config.smart_money.portfolio_retention_days,
                supabase_sync_enabled=self.config.data.supabase_sync_enabled,
            )
            self.trading_filter = TradingFilter(self.config.smart_money, self.config.trader)
            logger.info("Smart_Money_Portfolio pipeline initialized")
        except Exception as e:
            logger.warning(f"Smart_Money_Portfolio init failed (continuing without): {e}")
            self.holdings_scraper = None
            self.portfolio_scorer = None
            self.entry_calculator = None
            self.portfolio_store = None
            self.trading_filter = None

        # --- Qwen agent graceful degradation ---
        # If DASHSCOPE_API_KEY is missing/empty but qwen_enabled=True,
        # log a warning and disable Qwen so downstream components skip init.
        if self.config.qwen_agent.qwen_enabled:
            if not os.environ.get("DASHSCOPE_API_KEY"):
                logger.warning(
                    "DASHSCOPE_API_KEY is not set — disabling Qwen agent "
                    "(operating as qwen_enabled=False)"
                )
                self.config.qwen_agent.qwen_enabled = False
            else:
                # API key is present and qwen_enabled=True — init Qwen components
                try:
                    from smart_trader.qwen.client import QwenClient
                    from smart_trader.qwen.signal_arbitrator import SignalArbitrator
                    from smart_trader.qwen.catalyst_classifier import CatalystClassifier

                    qwen_client = QwenClient(self.config.qwen_agent)
                    self.signal_arbitrator = SignalArbitrator(
                        qwen_client, self.config.qwen_agent
                    )

                    # Inject CatalystClassifier into CatalystAnalyzer if it exists.
                    # Isolated so an injection failure degrades only catalyst
                    # classification — arbitration and commentary stay healthy.
                    if self.catalyst_analyzer is not None:
                        try:
                            catalyst_classifier = CatalystClassifier(
                                qwen_client, self.config.qwen_agent
                            )
                            self.catalyst_analyzer._classifier = catalyst_classifier
                            logger.info("Qwen CatalystClassifier injected into CatalystAnalyzer")
                        except Exception as e:
                            logger.warning(
                                f"CatalystClassifier injection failed "
                                f"(catalyst runs regex-only): {e}",
                                exc_info=True,
                            )
                            self._catalyst_init_error = f"classifier injection failed: {e}"
                    elif self.config.smart_money.catalyst_enabled:
                        # Classification is enabled but no analyzer exists to host the
                        # classifier — record why so the status is not a silent "failed".
                        self._catalyst_init_error = (
                            self._catalyst_init_error
                            or "CatalystAnalyzer unavailable (analyzer init failed)"
                        )

                    # Commentary Generator (runs in background thread per cycle)
                    if self.config.qwen_agent.commentary_enabled:
                        from smart_trader.qwen.commentary_generator import CommentaryGenerator
                        self.commentary_generator = CommentaryGenerator(
                            qwen_client, self.config.qwen_agent, api_state
                        )
                        logger.info("Qwen CommentaryGenerator initialized")

                    logger.info("Qwen agent components initialized (SignalArbitrator ready)")
                except Exception as e:
                    logger.warning(
                        f"Qwen agent init failed (continuing without): {e}",
                        exc_info=True,
                    )
                    # Reset ALL Qwen-backed components consistently — a failure here
                    # leaves none of them safely usable this session.
                    self.signal_arbitrator = None
                    self.commentary_generator = None
                    self._qwen_init_error = str(e)

        # Late-bind ohlcv_store into MockBroker for fill pricing
        if mock_mode and self.ohlcv_store is not None:
            self.client.set_ohlcv_store(self.ohlcv_store)

        snapshot = _load_snapshot(self.config)
        if snapshot:
            logger.info(f"Restored snapshot from {snapshot.get('saved_at')}")

        self._restore_exit_state()

        api_state.update(
            started_at=datetime.now().isoformat(),
            ibkr_connected=not mock_mode,
            ibkr_account=self.client.account_id,
            equity=equity,
            agent_commentary={
                "content": None,
                "timestamp": None,
                "cycle_number": None,
                "status": "unavailable",
            },
            agent_status={
                "catalyst": "disabled",
                "arbitration": "disabled",
                "commentary": "disabled",
                "last_cycle_timestamp": None,
            },
        )

        # Seed sample data before the first cycle so the dashboard isn't empty
        # on initial load (DEMO_SEED only; real data replaces it as it arrives).
        if self._demo_seed:
            logger.info("DEMO_SEED enabled — seeding sample dashboard data")
            self._apply_demo_overlay()

        self._start_api_server()
        self._running = True
        self._setup_signal_handlers()
        self._run_loop()

    # --------------------------------------------------------------- loop

    def _run_loop(self) -> None:
        logger.info("Trading loop started.")
        while self._running:
            try:
                if not self.client.ensure_connection():
                    logger.error("Connection lost, retry in 30s...")
                    time.sleep(30)
                    continue
                self._cycle()
                self._wait_next_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                time.sleep(60)
        self._shutdown()

    def _cycle(self) -> None:
        logger.info("-" * 40)
        cycle_start = time.time()
        equity = self.client.get_portfolio_value()

        positions = self.positions_tracker.get_positions()

        # Symbols with a still-working order (e.g. an unfilled RTH-only limit
        # entry waiting for the open). These are NOT yet held positions, so
        # without this guard the scanner would re-approve and resubmit a fresh
        # bracket for the same name every cycle, stacking duplicate orders.
        try:
            working_order_syms = self.executor.get_working_order_symbols()
        except Exception as e:
            logger.warning(f"Failed to fetch working order symbols: {e}")
            working_order_syms = set()
        if working_order_syms:
            logger.info(
                f"  {len(working_order_syms)} symbol(s) have working orders "
                f"(entry-blocked): {sorted(working_order_syms)}"
            )

        # Bookkeeping for signal-driven exit guards. Record open timestamps
        # for new positions, drop entries for closed ones, and GC expired
        # cooldowns so the dicts don't grow without bound. Pre-existing
        # positions on first cycle are recorded with `now` — they become
        # eligible for signal-driven exit only after min_holding_period_days
        # have elapsed since startup. Restart-with-stale-positions is a
        # known false-permissive trade-off; see __init__ comment.
        now_ts = datetime.now()
        current_syms = set(positions.keys())
        for sym in current_syms - set(self._position_open_ts.keys()):
            self._position_open_ts[sym] = now_ts
        for sym in set(self._position_open_ts.keys()) - current_syms:
            self._position_open_ts.pop(sym, None)
        cooldown_days = self.config.exits.reentry_cooldown_days
        if cooldown_days > 0:
            cooldown_horizon = now_ts - timedelta(days=cooldown_days)
            for sym in [s for s, t in self._recent_exits.items() if t < cooldown_horizon]:
                self._recent_exits.pop(sym, None)
        else:
            self._recent_exits.clear()

        # Resolve sectors for all held positions so the risk manager
        # has accurate sector data (not "unknown").
        if self.sector_resolver:
            self.sector_resolver.resolve_batch(current_syms)
            self.config.risk.sector_map.update(self.sector_resolver.get_map())

        portfolio_state = PortfolioState(
            equity=equity,
            cash=self.client.get_buying_power(),
            buying_power=self.client.get_buying_power(),
            positions={
                s: {
                    "market_value": p.get("market_value", 0),
                    "sector": self.config.risk.sector_map.get(s, "unknown"),
                }
                for s, p in positions.items()
            },
            peak_equity=self.risk.circuit_breaker.peak_equity,
            flicker_rate=0.0,
        )

        # --- Smart_Money_Portfolio refresh (daily cadence) ---
        self._maybe_refresh_portfolio()
        top_n_stocks, top_n_symbols, portfolio_symbols = self._current_top_n()
        self._update_top_n_history(top_n_symbols, current_syms)

        # Phase 3: refresh conviction scores for held symbols and update the
        # below-threshold history. Returns None when no provider had data
        # this cycle — bookkeeping skips, drop-out and decay both treat
        # outages as no-observation rather than as a signal.
        if self.scanner is not None and current_syms:
            try:
                held_conviction = self.scanner.get_held_position_conviction(current_syms)
            except Exception as e:
                logger.error(f"Conviction-scan failed: {e}", exc_info=True)
                held_conviction = None
        else:
            held_conviction = None
        self._update_conviction_history(held_conviction, current_syms)

        # --- Smart-money candidates ---
        candidates = []
        candidate_signals: List[Signal] = []
        if self.scanner is not None:
            try:
                candidates = self.scanner.get_candidates(
                    vol_rank=self.config.trader.scanner_vol_rank
                )
            except Exception as e:
                logger.error(f"SmartMoneyScanner failed: {e}", exc_info=True)
                candidates = []

            # Path B: filter scanner candidates to Top_N_Set (Req 5.3).
            # Fall back to unfiltered (Path C) when portfolio is empty or disabled.
            if (
                self.config.smart_money.portfolio_filter_enabled
                and self.trading_filter is not None
                and top_n_symbols
            ):
                outcomes = self.trading_filter.filter_scanner_candidates(
                    candidates, portfolio_symbols, top_n_symbols
                )
                dropped_reasons: Dict[str, int] = {}
                kept_symbols = set()
                for o in outcomes:
                    if o.kept:
                        kept_symbols.add(o.symbol)
                    else:
                        dropped_reasons[o.reason] = dropped_reasons.get(o.reason, 0) + 1
                if dropped_reasons:
                    logger.info(f"  Path B filter dropped: {dropped_reasons}")
                candidates = [c for c in candidates if c.symbol in kept_symbols]

            # Path B — standalone scanner entries. Disabled by default: smart
            # money is a SUPPLEMENT to the technical swing primary path, not an
            # independent entry trigger. `candidates` still flows to the
            # technical path's confidence bonus (candidates_by_sym below) and to
            # the dashboard regardless of this flag.
            if self.config.smart_money.scanner_standalone_entries_enabled:
                held = set(positions.keys())
                cooldown_set = set(self._recent_exits.keys())
                fresh = [
                    c for c in candidates
                    if c.symbol not in held
                    and c.symbol not in cooldown_set
                    and c.symbol not in working_order_syms
                    and c.conviction_score >= self.config.trader.min_conviction_score
                ]
                blocked_by_working = sum(
                    1 for c in candidates
                    if c.symbol in working_order_syms
                    and c.symbol not in held
                    and c.symbol not in cooldown_set
                    and c.conviction_score >= self.config.trader.min_conviction_score
                )
                if blocked_by_working:
                    logger.info(
                        f"  Working-order guard blocked {blocked_by_working} "
                        f"candidate(s) with unfilled orders"
                    )
                blocked_by_cooldown = sum(
                    1 for c in candidates
                    if c.symbol in cooldown_set
                    and c.symbol not in held
                    and c.conviction_score >= self.config.trader.min_conviction_score
                )
                if blocked_by_cooldown:
                    logger.info(
                        f"  Re-entry cooldown blocked {blocked_by_cooldown} candidate(s)"
                    )
                fresh.sort(key=lambda c: c.conviction_score, reverse=True)
                fresh = fresh[: self.config.trader.max_new_positions_per_cycle]

                for c in fresh:
                    entry_price = self._get_latest_price(c.symbol)
                    if entry_price is None or entry_price <= 0:
                        logger.warning(f"  Skipping {c.symbol}: no price available")
                        continue
                    sig = Signal(
                        symbol=c.symbol,
                        direction="LONG",
                        confidence=min(1.0, c.conviction_score / 10.0),
                        entry_price=entry_price,
                        stop_loss=entry_price * (1 - self.config.trader.default_stop_pct),
                        take_profit=entry_price * (1 + self.config.trader.default_take_profit_pct),
                        trailing_stop_pct=self.config.trader.default_trail_pct,
                        position_size_pct=0.0,  # risk_manager sizes from risk_per_trade
                        leverage=1.0,
                        timestamp=datetime.now(),
                        reasoning=(
                            f"SmartMoney: conviction {c.conviction_score:.2f} from "
                            f"{len(c.sources)} source(s) [{', '.join(c.sources)}]"
                        ),
                        strategy_name="SmartMoney",
                        metadata={
                            "smart_money": True,
                            "conviction_score": c.conviction_score,
                            "sources": c.sources,
                            "actors": c.actors,
                            "most_recent_filing": c.most_recent_filing.isoformat(),
                        },
                    )
                    candidate_signals.append(sig)
            elif candidates:
                logger.info(
                    f"  Scanner standalone entries OFF — {len(candidates)} candidate(s) "
                    f"used only as a technical-confidence supplement"
                )

        # --- Overlap + Path A: Entry_Calculator trigger signals ---
        overlap_signals: List[Signal] = []
        entry_signals: List[Signal] = []
        if self.trading_filter is not None and top_n_stocks:
            # Treat cooldown symbols and names with a working (unfilled) order
            # like held — neither path generates a new entry on a name we just
            # signal-driven-exited or already have an order working for.
            held_symbols = set(positions.keys())
            entry_blocked = held_symbols | set(self._recent_exits.keys()) | working_order_syms
            # Price lookups for every top-N stock (needed for both overlap + Path A)
            current_prices: Dict[str, float] = {}
            for stock in top_n_stocks:
                if stock.symbol in entry_blocked:
                    continue
                price = self._get_latest_price(stock.symbol)
                if price is not None and price > 0:
                    current_prices[stock.symbol] = price

            # Overlap: Path A AND Path B both fire for the same symbol → boosted signal
            overlap_signals = self.trading_filter.generate_overlap_signals(
                top_n=top_n_stocks,
                scanner_candidates=candidates,
                held_symbols=entry_blocked,
                current_prices=current_prices,
                min_conviction_score=self.config.trader.min_conviction_score,
            )
            overlap_syms = {s.symbol for s in overlap_signals}

            # Strip overlap symbols from scanner candidate_signals to avoid double-entry
            if overlap_syms:
                candidate_signals = [s for s in candidate_signals if s.symbol not in overlap_syms]

            # Path A for stocks with no overlap (and no existing scanner signal)
            scanner_symbols = {sig.symbol for sig in candidate_signals}
            entry_signals = self.trading_filter.generate_entry_signals(
                [
                    s for s in top_n_stocks
                    if s.symbol in current_prices
                    and s.symbol not in overlap_syms
                    and s.symbol not in scanner_symbols
                ],
                entry_blocked,
                current_prices,
            )

        # --- Ladder-in signals ---
        current_position_symbols = set(positions.keys())
        for closed_sym in self._previous_position_symbols - current_position_symbols:
            self.ladder_engine.clear_symbol(closed_sym)

        ladder_signals: List[Signal] = []
        bars_by_symbol: Dict[str, Any] = {}
        for sym in positions.keys():
            try:
                bars_by_symbol[sym] = self.market_data.get_historical_bars(
                    sym, duration="6 M", bar_size="1 day"
                )
            except Exception:
                continue
        ladder_signals = self.ladder_engine.evaluate_positions(
            positions, bars_by_symbol
        )

        self._previous_position_symbols = current_position_symbols

        # --- Signal-driven exits ---
        # Detect SELL/DECREASE filings from the same providers used for entry,
        # restricted to currently held positions. Phase 2 routes the resulting
        # FLAT signals through RiskManager.validate_exit_signal and either
        # logs (dry_run) or submits a market close after cancelling the
        # symbol's existing bracket children. Apply min-holding-period and
        # re-entry cooldown both — guards run regardless of dry_run so the
        # dry-run preview matches live behavior.
        exit_signals = self._evaluate_exit_signals(positions, top_n_symbols)
        for sig in exit_signals:
            decision = self.risk.validate_exit_signal(sig)
            if not decision.approved:
                logger.warning(
                    f"[EXIT REJECTED] {sig.symbol}: {decision.rejection_reason}"
                )
                sig.metadata["risk_rejected"] = True
                sig.metadata["rejection_reason"] = decision.rejection_reason
                continue

            # Record the cooldown the moment we DECIDE to exit, not after the
            # fill confirms. Suppresses re-entry on the very next cycle even
            # if the close is still in flight or the dry-run preview re-fires.
            self._recent_exits[sig.symbol] = datetime.now()

            # Per-trigger dry-run: a trigger can be in preview mode even when
            # the global flag is off (used to validate Phase 2b drop-outs
            # while Phase 2a sells run live). The dropout helper sets this
            # via metadata.dry_run; smart-money-sell uses the global flag.
            trigger_dry_run = bool(sig.metadata.get("dry_run", False))
            if trigger_dry_run or self.dry_run:
                logger.warning(
                    f"[DRY-RUN EXIT/{sig.metadata.get('exit_trigger', '?')}] "
                    f"{sig.symbol}: {sig.reasoning}"
                )
                continue

            try:
                cancelled = self.executor.cancel_orders_for_symbol(sig.symbol)
                if cancelled:
                    logger.info(
                        f"[EXIT] {sig.symbol}: cancelled {cancelled} bracket order(s)"
                    )
                trade = self.executor.close_position(sig.symbol)
            except Exception as e:
                logger.error(
                    f"[EXIT FAILED] {sig.symbol}: {e}", exc_info=True
                )
                sig.metadata["close_failed"] = True
                sig.metadata["close_error"] = str(e)
                continue

            if trade is None:
                logger.warning(
                    f"[EXIT] {sig.symbol}: close_position returned None "
                    "(position already closed or zero quantity)"
                )
                sig.metadata["close_skipped"] = True
            else:
                logger.warning(
                    f"[EXIT SUBMITTED] {sig.symbol}: market close placed. {sig.reasoning}"
                )
                sig.metadata["close_submitted"] = True
                # Drop ladder state so a future re-entry starts fresh.
                if self.ladder_engine is not None:
                    self.ladder_engine.clear_symbol(sig.symbol)

        # --- Technical primary signals (Humbled Trader: 200 SMA + 8 EMA / gap up) ---
        # These are the PRIMARY entry gate; smart money data is used only as a
        # confidence scoring bonus. Run before Path A / Overlap so the technical
        # path gets first priority in the RiskManager concurrency cap.
        technical_primary_signals: List[Signal] = []
        sm_cfg = self.config.smart_money
        if (
            self.entry_calculator is not None
            and self.trading_filter is not None
            and self._latest_scored
            and (sm_cfg.ema_pullback_enabled or sm_cfg.gap_up_enabled)
        ):
            tech_universe = [s.symbol for s in self._latest_scored]
            # compute_technical() falls back to the latest bar close for any symbol
            # not supplied here, so passing an empty dict is safe.
            tech_prices: Dict[str, float] = {}
            try:
                tech_signals_map = self.entry_calculator.compute_technical(
                    tech_universe, tech_prices, sm_cfg
                )
                scored_by_sym = {s.symbol: s for s in self._latest_scored}
                candidates_by_sym = {c.symbol: c for c in candidates}
                tech_blocked = set(positions.keys()) | set(self._recent_exits.keys())

                # Catalyst analysis for all technically triggered symbols
                catalyst_data: Dict[str, list] = {}
                if self.catalyst_analyzer is not None:
                    triggered_syms = [
                        sym for sym, ts in tech_signals_map.items()
                        if ts.signal_type != "none"
                    ]
                    if triggered_syms:
                        try:
                            catalyst_data = self.catalyst_analyzer.analyze(triggered_syms)
                            catalyst_found = sum(
                                1 for evts in catalyst_data.values() if evts
                            )
                            logger.info(
                                f"  Catalyst: {catalyst_found}/{len(triggered_syms)} "
                                f"triggered symbols have news"
                            )
                        except Exception as e:
                            logger.warning(
                                f"CatalystAnalyzer failed: {e} — proceeding without catalyst data"
                            )

                technical_primary_signals = self.trading_filter.generate_technical_primary_signals(
                    tech_signals_map, scored_by_sym, candidates_by_sym, tech_blocked,
                    catalyst_events=catalyst_data,
                )
                logger.info(
                    f"  Technical primary: {len(technical_primary_signals)} signal(s) from "
                    f"{len(tech_universe)}-symbol universe"
                )
            except Exception as e:
                logger.error(f"Technical primary scan failed: {e}", exc_info=True)

        # --- Market regime gate: block NEW entries in an unfavorable regime ---
        # Existing positions and exits are never gated; ladder adds (DCA into
        # held positions) pass through too — only fresh entries are suppressed.
        # regime_state is None when the filter is disabled or data is missing,
        # which FAILS OPEN (entries allowed) so a data gap never halts trading.
        regime_state = self._refresh_regime()
        regime_entries_allowed = regime_state is None or regime_state.entries_allowed
        new_entry_signals = (
            technical_primary_signals + overlap_signals + entry_signals + candidate_signals
        )
        if not regime_entries_allowed:
            if new_entry_signals:
                logger.info(
                    f"  Regime gate: entries BLOCKED ({regime_state.zone}, "
                    f"posture={regime_state.posture}) — {len(new_entry_signals)} "
                    f"new-entry signal(s) suppressed; ladder/exits still active"
                )
            new_entry_signals = []

        # --- Signal Arbitration (Qwen-powered ranking) ---
        # If the Signal Arbitrator is enabled and we have ≥2 candidate signals,
        # ask Qwen to rank them by priority given portfolio context.
        # On failure or bypass: signals pass through in original order.
        if self.signal_arbitrator is not None and len(new_entry_signals) >= 2:
            regime_state_dict = regime_state.to_dict() if regime_state is not None else None
            try:
                new_entry_signals = self.signal_arbitrator.rank(
                    new_entry_signals, portfolio_state, regime_state_dict
                )
                logger.info(
                    f"  Signal arbitration: ranked {len(new_entry_signals)} signal(s)"
                )
            except Exception as e:
                logger.warning(
                    f"Signal arbitration failed (using original order): {e}"
                )

        # --- Validate + execute all signals ---
        # Order: technical primary (Humbled Trader setup) first, then overlap (2x sizing),
        # then Path A (entry-trigger), then Path B (scanner), then ladder adds.
        # Dedup by symbol — first signal to reach RiskManager for a given symbol wins.
        # The risk manager's sector/concurrency caps favor whatever reaches it first.
        _seen_syms: set = set()
        _deduped: List[Signal] = []
        for _sig in new_entry_signals + ladder_signals:
            if _sig.symbol not in _seen_syms:
                _seen_syms.add(_sig.symbol)
                _deduped.append(_sig)
            else:
                logger.debug(
                    f"  signal dedup: {_sig.symbol} already queued by an earlier path "
                    f"({_sig.strategy_name} dropped)"
                )
        all_signals = _deduped

        # Pre-resolve sectors for all signal symbols so the risk manager
        # doesn't reject them as "unknown".
        if self.sector_resolver and all_signals:
            signal_syms = {sig.symbol for sig in all_signals}
            self.sector_resolver.resolve_batch(signal_syms)
            self.config.risk.sector_map.update(self.sector_resolver.get_map())

        signal_entries = []
        for sig in all_signals:
            decision = self.risk.validate_signal(sig, portfolio_state)
            if not decision.approved:
                logger.warning(f"[{sig.symbol}] REJECTED: {decision.rejection_reason}")
                signal_entries.append(self._signal_entry(sig, 0, "rejected", decision))
                continue

            modified = decision.modified_signal
            for mod in decision.modifications:
                logger.info(f"[{sig.symbol}] modified: {mod}")

            # Ladder-in signals carry a fixed share count in metadata
            if modified.metadata.get("ladder_in"):
                shares = modified.metadata.get("additional_shares", 0)
            else:
                # Overlap signals boost risk_per_trade by `risk_multiplier`
                # (default 2.0 from TradingFilter.generate_overlap_signals).
                risk_mult = modified.metadata.get("risk_multiplier")
                risk_pct_override = (
                    self.config.risk.max_risk_per_trade * float(risk_mult)
                    if risk_mult is not None
                    else None
                )
                shares = self.risk.compute_position_size(
                    modified.entry_price,
                    modified.stop_loss,
                    equity,
                    risk_pct_override=risk_pct_override,
                )

            status = "approved" if shares > 0 else "skipped"
            signal_entries.append(self._signal_entry(modified, shares, status, decision))
            if shares <= 0:
                continue

            if self.dry_run:
                logger.info(
                    f"[DRY-RUN] Would {modified.direction} {modified.symbol} "
                    f"{shares} shares @ ${modified.entry_price:.2f}"
                )
                continue

            ibkr_action = "BUY" if modified.direction == "LONG" else "SELL"
            take_profit = modified.take_profit or modified.entry_price * (
                1 + self.config.trader.default_take_profit_pct
            )

            if modified.trailing_stop_pct:
                logger.info(
                    f"{ibkr_action} {shares} {modified.symbol} @ ${modified.entry_price:.2f} "
                    f"(trail {modified.trailing_stop_pct:.1%})"
                )
                self.executor.submit_bracket_with_trailing_stop(
                    modified.symbol, shares, ibkr_action,
                    entry_price=modified.entry_price,
                    hard_stop=modified.stop_loss,
                    trailing_percent=modified.trailing_stop_pct * 100.0,
                    take_profit=take_profit,
                )
            else:
                logger.info(f"{ibkr_action} {shares} {modified.symbol} @ ${modified.entry_price:.2f}")
                self.executor.submit_bracket_order(
                    modified.symbol, shares, ibkr_action,
                    entry_price=modified.entry_price,
                    stop_loss=modified.stop_loss,
                    take_profit=take_profit,
                )

        # --- Commentary Generation (background, non-blocking) ---
        self._cycle_count += 1
        if (
            self.commentary_generator is not None
            and self.config.qwen_agent.commentary_enabled
        ):
            cycle_context = {
                "signals_generated": [
                    {
                        "symbol": sig.symbol,
                        "direction": sig.direction,
                        "confidence": sig.confidence,
                    }
                    for sig in all_signals
                ],
                "signals_approved": [
                    entry for entry in signal_entries if entry["status"] == "approved"
                ],
                "signals_rejected": [
                    {"symbol": entry["symbol"], "reason": entry["rejection_reason"]}
                    for entry in signal_entries
                    if entry["status"] == "rejected"
                ],
                "positions": positions,
                "equity": equity,
                "cash": portfolio_state.cash,
                "cycle_number": self._cycle_count,
            }
            self.commentary_generator.generate_async(cycle_context)

        # --- Update agent_status in api_state ---
        agent_status = {
            "catalyst": "disabled",
            "arbitration": "disabled",
            "commentary": "disabled",
            "last_cycle_timestamp": datetime.now().isoformat(),
        }
        if self.config.qwen_agent.qwen_enabled:
            # Catalyst status. "failed" is reserved for genuine wiring errors;
            # a source that is simply switched off reports "disabled" so the
            # dashboard never shows a false alarm.
            if self.config.qwen_agent.catalyst_classification_enabled:
                classifier_wired = (
                    self.catalyst_analyzer is not None
                    and getattr(self.catalyst_analyzer, "_classifier", None) is not None
                )
                if not self.config.smart_money.catalyst_enabled:
                    # News source itself is off — classification has nothing to run on.
                    agent_status["catalyst"] = "disabled"
                elif classifier_wired:
                    agent_status["catalyst"] = "succeeded"
                else:
                    agent_status["catalyst"] = "failed"
            # Arbitration status
            if self.config.qwen_agent.signal_arbitration_enabled:
                agent_status["arbitration"] = (
                    "succeeded" if self.signal_arbitrator is not None else "failed"
                )
            # Commentary status
            if self.config.qwen_agent.commentary_enabled:
                agent_status["commentary"] = (
                    "succeeded" if self.commentary_generator is not None else "failed"
                )
            # Attach a concrete reason for any "failed" component so the failure is
            # diagnosable from the API/dashboard, not just the server logs.
            if agent_status["catalyst"] == "failed":
                agent_status["catalyst_error"] = (
                    self._catalyst_init_error
                    or self._qwen_init_error
                    or "CatalystClassifier not wired (see server logs)"
                )
            if "failed" in (agent_status["arbitration"], agent_status["commentary"]):
                agent_status["qwen_error"] = (
                    self._qwen_init_error or "Qwen component not initialized (see server logs)"
                )
        api_state.update(agent_status=agent_status)

        # --- Push state to API ---
        try:
            stop_prices = self.executor.get_trail_stop_prices()
        except Exception as e:
            logger.warning(f"Failed to fetch trail stop prices: {e}")
            stop_prices = {}

        positions_list = [
            {
                "symbol": s,
                "quantity": p.get("quantity", 0),
                "avg_cost": p.get("avg_cost", 0),
                "market_price": p.get("market_price", 0),
                "market_value": p.get("market_value", 0),
                "unrealized_pnl": p.get("unrealized_pnl", 0),
                "stop_price": stop_prices.get(s),
            }
            for s, p in positions.items()
        ]

        for entry in signal_entries:
            api_state.append_signal(entry)

        next_cycle = datetime.now() + timedelta(
            seconds=self.config.trader.cycle_interval_seconds
        )
        api_state.update(
            equity=equity,
            cash=portfolio_state.cash,
            buying_power=portfolio_state.buying_power,
            daily_pnl=self.risk.circuit_breaker.current_equity
                      - self.risk.circuit_breaker.daily_start_equity,
            daily_pnl_pct=(
                (self.risk.circuit_breaker.current_equity - self.risk.circuit_breaker.daily_start_equity)
                / self.risk.circuit_breaker.daily_start_equity
                if self.risk.circuit_breaker.daily_start_equity else 0
            ),
            allocation_pct=portfolio_state.total_exposure_pct,
            positions=positions_list,
            risk_status=self.risk.get_risk_status(),
            ibkr_connected=self.client.is_connected,
            next_cycle_time=next_cycle.isoformat(),
            last_cycle_seconds=time.time() - cycle_start,
            alert_history=self.alerts.get_history(),
            smart_money_candidates=[
                {
                    "symbol": c.symbol,
                    "conviction_score": c.conviction_score,
                    "sources": c.sources,
                    "actors": c.actors,
                    "total_dollar_volume": c.total_dollar_volume,
                    "filing_count": c.filing_count,
                    "most_recent_filing": c.most_recent_filing.isoformat(),
                }
                for c in candidates[:20]
            ],
            pending_exits=[
                {
                    "symbol": s.symbol,
                    "trigger": s.metadata.get("exit_trigger", "unknown"),
                    "conviction_score": float(s.metadata.get("conviction_score", 0.0) or 0.0),
                    "sources": list(s.metadata.get("sources", [])),
                    "actors": list(s.metadata.get("actors", [])),
                    "filing_count": int(s.metadata.get("filing_count", 0) or 0),
                    "total_dollar_volume": float(s.metadata.get("total_dollar_volume", 0.0) or 0.0),
                    "most_recent_filing": s.metadata.get("most_recent_filing", ""),
                    "reasoning": s.reasoning,
                    "dry_run": bool(s.metadata.get("dry_run", True)),
                    "timestamp": s.timestamp.isoformat() if s.timestamp else "",
                }
                for s in exit_signals
            ],
            portfolio_snapshot=self._build_api_portfolio_snapshot(),
            regime=regime_state.to_dict() if regime_state is not None else None,
        )

        # Re-fill any sections the live cycle left empty (DEMO_SEED only).
        self._apply_demo_overlay()

        self._persist_exit_state()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _verify_live_opt_in(creds: dict, account_id: str) -> None:
        """Gate live-account trading behind a deliberate two-factor opt-in.

        Paper is always the default. Trading a LIVE account requires BOTH:
          - ALLOW_LIVE_TRADING truthy in .env, AND
          - LIVE_ACCOUNT_ID matching the connected account id.
        Either missing → abort. A single stray flag can never put real money at
        risk; real money only flows on a deliberate, two-part act.
        """
        actual = (account_id or "").strip()
        allow_live = bool(creds.get("allow_live_trading"))
        expected = (creds.get("live_account_id") or "").strip()

        if not allow_live:
            logger.critical(
                f"Safety check: connected to LIVE account {actual}, but live "
                "trading is not enabled. Set ALLOW_LIVE_TRADING=1 and "
                "LIVE_ACCOUNT_ID=<account> in .env to trade real money. Aborting."
            )
            sys.exit(1)
        if not expected or actual != expected:
            logger.critical(
                f"Safety check: live account {actual} does not match "
                f"LIVE_ACCOUNT_ID={expected or '(unset)'}. Aborting to avoid "
                "trading the wrong account."
            )
            sys.exit(1)

        logger.warning("=" * 60)
        logger.warning(f"  LIVE TRADING ENABLED — account {actual} — REAL MONEY AT RISK")
        logger.warning("=" * 60)

    def _refresh_regime(self):
        """Refresh the market-regime gate, cached once per ET trading day.

        Returns the current RegimeState (or None when the filter is disabled or
        data is unavailable). A None result means FAIL OPEN — callers must treat
        it as "entries allowed" so a data gap never silently halts trading.
        Daily bars don't change intraday, so we recompute at most once per day.
        """
        cfg = self.config.smart_money
        if not getattr(cfg, "regime_filter_enabled", False):
            return None

        today_et = datetime.now(_ET).date().isoformat()
        if self._regime_date == today_et and self._regime_state is not None:
            return self._regime_state
        if self.market_data is None:
            return self._regime_state  # keep last known (None → fail open)

        try:
            bars = self.market_data.get_historical_bars(
                cfg.regime_symbol, duration="2 Y", bar_size="1 day"
            )
            if bars is None or bars.empty or "close" not in bars.columns:
                logger.warning(f"  Regime: no bars for {cfg.regime_symbol} — failing open")
                return self._regime_state
            state = classify_regime(
                bars["close"],
                posture=cfg.regime_posture,
                slope_lookback=cfg.regime_slope_lookback,
            )
            if state is not None:
                self._regime_state = state
                self._regime_date = today_et
                logger.info(
                    f"  Regime [{cfg.regime_symbol}]: {state.zone.upper()} "
                    f"(posture={state.posture}, "
                    f"entries={'ON' if state.entries_allowed else 'OFF'}) "
                    f"close={state.close:.2f} 50SMA={state.sma_50:.2f} "
                    f"200SMA={state.sma_200:.2f}"
                )
            return state or self._regime_state
        except Exception as e:
            logger.warning(f"  Regime refresh failed ({e}) — failing open")
            return self._regime_state

    def _maybe_refresh_portfolio(self) -> None:
        """Refresh the Smart_Money_Portfolio once per NY trading day."""
        if self.holdings_scraper is None or self.portfolio_scorer is None:
            return
        today_et = datetime.now(_ET).date().isoformat()
        if self._last_portfolio_refresh_date == today_et and self._latest_scored:
            return
        try:
            holdings = self.holdings_scraper.fetch_all()
            if not holdings:
                logger.warning("  portfolio: no holdings available, keeping previous snapshot")
                return
            scored = self.portfolio_scorer.score(
                holdings, n_providers=self.holdings_scraper.n_enabled_providers
            )
            top_n_size = self.config.smart_money.top_n_size
            top_symbols = [s.symbol for s in scored[:top_n_size] if s.composite_score > 0]
            entry_prices = self.entry_calculator.compute(top_symbols) if self.entry_calculator else {}

            if self.portfolio_store is not None:
                try:
                    self.portfolio_store.save_snapshot(scored, top_n_size, entry_prices)
                except Exception as e:
                    logger.warning(f"  portfolio: save_snapshot failed: {e}")

            self._latest_scored = scored
            self._latest_entry_prices = entry_prices
            self._last_portfolio_refresh_date = today_et
            logger.info(
                f"  portfolio: refreshed — universe={len(scored)}, "
                f"top_n={len([s for s in scored[:top_n_size] if s.composite_score > 0])}"
            )
        except Exception as e:
            logger.error(f"  portfolio refresh failed: {e}", exc_info=True)

    def _current_top_n(self):
        """Return (tradable_top_n_stocks, tradable_symbols, portfolio_symbols).

        The returned list is filtered by the tradability rule (Req 5.7):
        a stock must either meet the minimum-overlap threshold OR have a
        single-fund concentration exception. Stocks that fail the rule stay
        in the persisted portfolio for visibility but cannot trigger trades.
        """
        if not self._latest_scored:
            return [], set(), set()
        sm = self.config.smart_money
        top_n_size = sm.top_n_size
        top_slice = [s for s in self._latest_scored[:top_n_size] if s.composite_score > 0]

        all_top_n: List[TopNStock] = []
        for s in top_slice:
            max_weight = max((f.holding_weight for f in s.funds), default=0.0)
            all_top_n.append(TopNStock(
                symbol=s.symbol,
                composite_score=s.composite_score,
                optimal_entry_price=self._latest_entry_prices.get(s.symbol),
                overlap_count=s.overlap_count,
                sources=sorted({f.provider_name for f in s.funds}),
                max_single_fund_weight=max_weight,
            ))

        # Tradability rule: overlap OR single-fund concentration exception
        min_overlap = sm.min_overlap_count_for_trading
        concentration_exception = sm.single_fund_concentration_exception
        tradable: List[TopNStock] = []
        blocked: List[str] = []
        for s in all_top_n:
            if is_tradable(s, min_overlap, concentration_exception):
                tradable.append(s)
            else:
                blocked.append(s.symbol)

        if blocked:
            logger.info(
                f"  tradability rule: blocked {len(blocked)} low-conviction picks "
                f"(overlap<{min_overlap} AND max_weight<{concentration_exception:.0%}): {blocked}"
            )

        tradable_symbols = {s.symbol for s in tradable}
        portfolio_symbols = {s.symbol for s in self._latest_scored}
        return tradable, tradable_symbols, portfolio_symbols

    def _build_api_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
        """Serialize the current portfolio into the dict form api/server.py expects."""
        if not self._latest_scored:
            return None
        top_n_size = self.config.smart_money.top_n_size
        stocks = []
        for rank, s in enumerate(self._latest_scored, start=1):
            in_top_n = rank <= top_n_size and s.composite_score > 0
            entry = self._latest_entry_prices.get(s.symbol) if in_top_n else None
            # current_price is best-effort — skip fetching here; API layer will
            # see the last bar via positions or its own lookup.
            stocks.append({
                "symbol": s.symbol,
                "rank": rank,
                "in_top_n": in_top_n,
                "composite_score": s.composite_score,
                "overlap_count": s.overlap_count,
                "average_holding_weight": s.average_holding_weight,
                "performance_score": s.performance_score,
                "momentum_score": s.momentum_score,
                "relative_strength": s.relative_strength,
                "optimal_entry_price": entry,
                "current_price": None,
                "funds": [
                    {
                        "fund_name": f.fund_name,
                        "provider_name": f.provider_name,
                        "holding_weight": f.holding_weight,
                        "share_count": f.share_count,
                        "market_value": f.market_value,
                        "as_of_date": f.as_of_date.isoformat(),
                    }
                    for f in s.funds
                ],
            })
        return {
            "generated_at": datetime.now().isoformat(),
            "top_n_size": top_n_size,
            "universe_size": len(self._latest_scored),
            "stocks": stocks,
        }


    def _restore_exit_state(self) -> None:
        """Hydrate the four exit-state dicts from PortfolioStore at startup.

        Best-effort: any failure logs and continues with empty dicts (same
        behavior as a first run). Skipped silently when portfolio_store
        wasn't initialized — exits then run with the prior in-memory-only
        behavior.
        """
        if self.portfolio_store is None:
            return
        try:
            state = self.portfolio_store.load_exit_state()
        except Exception as e:
            logger.warning(f"Restore exit state failed (continuing fresh): {e}")
            return
        self._position_open_ts = state.get("position_open_ts", {})
        self._recent_exits = state.get("recent_exits", {})
        self._held_top_n_history = state.get("top_n_history", {})
        self._held_conviction_below_since = state.get("conviction_below_since", {})
        logger.info(
            "Restored exit state: "
            f"{len(self._position_open_ts)} open positions, "
            f"{len(self._recent_exits)} cooldowns, "
            f"{len(self._held_top_n_history)} top-N observations, "
            f"{len(self._held_conviction_below_since)} conviction-below entries"
        )

    def _persist_exit_state(self) -> None:
        """Snapshot the four exit-state dicts to PortfolioStore.

        Whole-dict replace per cycle — cheap (4 small JSON blobs). Failure
        is logged but never aborts the cycle: stale persistence is far less
        bad than a crashed trading loop.
        """
        if self.portfolio_store is None:
            return
        try:
            self.portfolio_store.save_exit_state({
                "position_open_ts": self._position_open_ts,
                "recent_exits": self._recent_exits,
                "top_n_history": self._held_top_n_history,
                "conviction_below_since": self._held_conviction_below_since,
            })
        except Exception as e:
            logger.warning(f"Persist exit state failed: {e}")

    def _update_conviction_history(self, scores, held_symbols) -> None:
        """Track when each eligible held symbol first dropped below the
        conviction floor. Only tracks symbols previously observed in top-N
        (manual holds and pre-bot positions are exempt). No-op when scores
        is None — outage shouldn't fabricate a portfolio-wide decay."""
        if scores is None:
            return
        threshold = self.config.exits.min_held_conviction_score
        now_ts = datetime.now()
        held_set = set(held_symbols)
        for sym in held_set:
            score = scores.get(sym, 0.0)
            if score >= threshold:
                self._held_conviction_below_since.pop(sym, None)
                continue
            # Below threshold — only start the clock if this symbol is in
            # the top-N eligibility set. Don't override an existing entry
            # (we want the FIRST drop time, not the most recent).
            if sym in self._held_top_n_history:
                self._held_conviction_below_since.setdefault(sym, now_ts)
        for sym in list(self._held_conviction_below_since.keys()):
            if sym not in held_set:
                self._held_conviction_below_since.pop(sym, None)

    def _update_top_n_history(self, top_n_symbols, held_symbols) -> None:
        """Track when each held symbol was last observed in a valid top-N.

        No-op when top-N is empty (pipeline failure or pre-first-refresh) —
        a transient empty top-N would otherwise fabricate drop-outs for
        every held symbol. Drops history entries for symbols no longer
        held, so the dict stays bounded to the current portfolio.
        """
        if not top_n_symbols:
            return
        now_ts = datetime.now()
        held_set = set(held_symbols)
        top_set = set(top_n_symbols)
        for sym in held_set & top_set:
            self._held_top_n_history[sym] = now_ts
        for sym in list(self._held_top_n_history.keys()):
            if sym not in held_set:
                self._held_top_n_history.pop(sym, None)

    def _passes_holding_period(self, symbol: str, now_ts: datetime) -> bool:
        """Shared min-holding-period guard for all exit triggers."""
        min_hold_days = self.config.exits.min_holding_period_days
        if min_hold_days <= 0:
            return True
        opened_at = self._position_open_ts.get(symbol)
        if opened_at is None:
            return True  # Unknown open time — let the exit through.
        held_days = (now_ts - opened_at).total_seconds() / 86400.0
        if held_days < min_hold_days:
            logger.info(
                f"  Exit blocked for {symbol}: held {held_days:.1f}d "
                f"< min_holding_period_days={min_hold_days}"
            )
            return False
        return True

    def _smart_money_sell_exits(self, positions: Dict[str, Any]) -> List[Signal]:
        """SELL/DECREASE filings on held positions → FLAT signals."""
        if (
            not self.config.exits.on_smart_money_sell
            or self.scanner is None
            or not positions
        ):
            return []
        try:
            sell_candidates = self.scanner.get_held_position_sells(set(positions.keys()))
        except Exception as e:
            logger.error(f"Exit scan failed: {e}", exc_info=True)
            return []

        floor = self.config.exits.min_exit_conviction_score
        now_ts = datetime.now()
        out: List[Signal] = []
        for c in sell_candidates:
            if c.conviction_score < floor:
                continue
            if not self._passes_holding_period(c.symbol, now_ts):
                continue
            last_price = self._get_latest_price(c.symbol) or 0.0
            out.append(Signal(
                symbol=c.symbol,
                direction="FLAT",
                confidence=min(1.0, c.conviction_score / 10.0),
                entry_price=last_price,
                stop_loss=0.0,
                take_profit=None,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=now_ts,
                reasoning=(
                    f"SmartMoney SELL: conviction {c.conviction_score:.2f} from "
                    f"{len(c.sources)} source(s) [{', '.join(c.sources)}]"
                ),
                strategy_name="SmartMoneyExit",
                metadata={
                    "exit_signal": True,
                    "exit_trigger": "smart_money_sell",
                    "conviction_score": c.conviction_score,
                    "sources": c.sources,
                    "actors": c.actors,
                    "filing_count": c.filing_count,
                    "total_dollar_volume": c.total_dollar_volume,
                    "most_recent_filing": c.most_recent_filing.isoformat(),
                    "dry_run": self.config.exits.dry_run,
                },
            ))
        return out

    def _top_n_dropout_exits(
        self, positions: Dict[str, Any], top_n_symbols
    ) -> List[Signal]:
        """Held symbols out of top-N for ≥ N consecutive days → FLAT signals.

        Skips symbols never observed in top-N (no history → not a drop-out,
        could be a manual hold). Skips when current top-N is empty (avoids
        cascading drop-outs on a transient pipeline gap)."""
        if not self.config.exits.on_top_n_dropout or not positions:
            return []
        if not top_n_symbols:
            return []

        days_required = self.config.exits.top_n_dropout_days_required
        now_ts = datetime.now()
        top_set = set(top_n_symbols)
        out: List[Signal] = []
        for sym in positions.keys():
            if sym in top_set:
                continue
            last_seen = self._held_top_n_history.get(sym)
            if last_seen is None:
                continue  # never observed in top-N → not a drop-out
            days_out = (now_ts - last_seen).total_seconds() / 86400.0
            if days_out < days_required:
                continue
            if not self._passes_holding_period(sym, now_ts):
                continue
            last_price = self._get_latest_price(sym) or 0.0
            out.append(Signal(
                symbol=sym,
                direction="FLAT",
                confidence=0.5,
                entry_price=last_price,
                stop_loss=0.0,
                take_profit=None,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=now_ts,
                reasoning=(
                    f"Top-N drop-out: out for {days_out:.1f}d "
                    f">= {days_required}d threshold"
                ),
                strategy_name="SmartMoneyExit",
                metadata={
                    "exit_signal": True,
                    "exit_trigger": "top_n_dropout",
                    "days_out_of_top_n": days_out,
                    "last_seen_in_top_n": last_seen.isoformat(),
                    "dry_run": (
                        self.config.exits.dry_run
                        or self.config.exits.top_n_dropout_dry_run
                    ),
                },
            ))
        return out

    def _conviction_decay_exits(self, positions: Dict[str, Any]) -> List[Signal]:
        """Held symbols whose conviction has been below threshold for
        ≥ `conviction_decay_days_required` days → FLAT signals. Eligibility
        and observation are managed by `_update_conviction_history`; this
        helper just reads `_held_conviction_below_since` and emits."""
        if not self.config.exits.on_conviction_decay or not positions:
            return []
        days_required = self.config.exits.conviction_decay_days_required
        now_ts = datetime.now()
        out: List[Signal] = []
        for sym in positions.keys():
            below_since = self._held_conviction_below_since.get(sym)
            if below_since is None:
                continue
            days_below = (now_ts - below_since).total_seconds() / 86400.0
            if days_below < days_required:
                continue
            if not self._passes_holding_period(sym, now_ts):
                continue
            last_price = self._get_latest_price(sym) or 0.0
            out.append(Signal(
                symbol=sym,
                direction="FLAT",
                confidence=0.5,
                entry_price=last_price,
                stop_loss=0.0,
                take_profit=None,
                position_size_pct=0.0,
                leverage=1.0,
                timestamp=now_ts,
                reasoning=(
                    f"Conviction decay: below "
                    f"{self.config.exits.min_held_conviction_score:.2f} for "
                    f"{days_below:.1f}d >= {days_required}d threshold"
                ),
                strategy_name="SmartMoneyExit",
                metadata={
                    "exit_signal": True,
                    "exit_trigger": "conviction_decay",
                    "days_below_threshold": days_below,
                    "below_since": below_since.isoformat(),
                    "min_held_conviction_score": (
                        self.config.exits.min_held_conviction_score
                    ),
                    "dry_run": (
                        self.config.exits.dry_run
                        or self.config.exits.conviction_decay_dry_run
                    ),
                },
            ))
        return out

    def _evaluate_exit_signals(
        self, positions: Dict[str, Any], top_n_symbols=None
    ) -> List[Signal]:
        """Combine all enabled exit triggers, dedupe by symbol.

        Priority when multiple triggers fire on one symbol:
        smart-money-sell > top-N drop-out > conviction-decay. Higher-priority
        triggers carry richer metadata (sources, actors, dollar volumes)
        which is more useful downstream than the structural reasons.
        """
        if not self.config.exits.enabled or not positions:
            return []
        sm_exits = self._smart_money_sell_exits(positions)
        seen = {s.symbol for s in sm_exits}
        dropout_exits = [
            s for s in self._top_n_dropout_exits(positions, top_n_symbols or set())
            if s.symbol not in seen
        ]
        seen.update(s.symbol for s in dropout_exits)
        decay_exits = [
            s for s in self._conviction_decay_exits(positions)
            if s.symbol not in seen
        ]
        return sm_exits + dropout_exits + decay_exits

    def _get_latest_price(self, symbol: str) -> Optional[float]:
        try:
            bars = self.market_data.get_historical_bars(
                symbol, duration="1 M", bar_size="1 day"
            )
            if bars is None or len(bars) == 0:
                return None
            return float(bars["close"].iloc[-1])
        except Exception as e:
            logger.debug(f"  price fetch failed for {symbol}: {e}")
            return None

    def _signal_entry(self, sig: Signal, shares: int, status: str, decision) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": sig.symbol,
            "direction": sig.direction,
            "strategy": sig.strategy_name,
            "size_pct": f"{sig.position_size_pct:.0%}",
            "shares": shares,
            "entry_price": round(sig.entry_price, 2),
            "stop_loss": round(sig.stop_loss, 2),
            "status": status,
            "reasoning": sig.reasoning[:120],
            "rejection_reason": (decision.rejection_reason or "")[:120],
            "modifications": decision.modifications[:3] if decision.modifications else [],
            "is_ladder_in": bool(sig.metadata.get("ladder_in")),
            "is_smart_money": bool(sig.metadata.get("smart_money")),
            "smart_money_conviction": float(sig.metadata.get("conviction_score", 0.0) or 0.0),
            "smart_money_sources": list(sig.metadata.get("sources", [])),
        }
        # Include arbitration reasoning if present (from Signal Arbitrator)
        arb_reasoning = sig.metadata.get("arbitration_reasoning")
        if arb_reasoning:
            entry["arbitration_reasoning"] = arb_reasoning
        return entry

    def _apply_demo_overlay(self) -> None:
        """Fill empty dashboard sections with sample data (DEMO_SEED only)."""
        if not self._demo_seed:
            return
        try:
            updates = demo_data.demo_overlay(api_state.snapshot())
            if updates:
                api_state.update(**updates)
        except Exception as e:
            logger.warning(f"Demo overlay failed (non-fatal): {e}")

    def _start_api_server(self) -> None:
        import uvicorn
        from smart_trader.api.server import app

        def _run():
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=self.config.monitoring.api_port,
                log_level="warning",
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logger.info(f"API server started on port {self.config.monitoring.api_port}")

    def _wait_next_cycle(self) -> None:
        time.sleep(self.config.trader.cycle_interval_seconds)

    def _setup_signal_handlers(self) -> None:
        def handler(signum, frame):
            logger.info(f"Signal {signum}, shutting down...")
            self._running = False

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _shutdown(self) -> None:
        logger.info("Shutting down Smart Trader...")
        _save_snapshot(
            self.config,
            {
                "saved_at": datetime.now().isoformat(),
                "peak_equity": self.risk.circuit_breaker.peak_equity if self.risk else None,
                "positions": list(self.positions_tracker.get_positions().keys())
                             if self.positions_tracker else [],
            },
        )
        if self.client and self.client.is_connected:
            self.client.disconnect()
        logger.info("Goodbye.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="smart_trader", description="HMM-free smart-money trading bot."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["live", "dry-run"],
        default="live",
        help="live (places orders) or dry-run (no orders)",
    )
    args = parser.parse_args()

    config = load_config()
    trader = SmartTrader(config, dry_run=(args.mode == "dry-run"))
    trader.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
