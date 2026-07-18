"""
Central configuration for smart-trader.

Only includes the components kept from the predecessor regime_trader project:
- BrokerConfig: IBKR connection
- RiskConfig: circuit breakers, position sizing, correlation
- SmartMoneyConfig: multi-source stock scanner
- LadderInConfig: DCA on dips for held positions
- MonitoringConfig: logging, API, alerts
- DataConfig: OHLCV cache (SQLite + optional Supabase)
- TraderConfig: new, orchestrator-level settings (cycle cadence, entry defaults)
"""
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class BrokerConfig:
    host: str = "127.0.0.1"
    port: int = 7497  # 7497 = TWS paper, 7496 = TWS live, 4002 = Gateway paper
    client_id: int = 1
    account: str = ""
    paper_trading: bool = True
    connection_timeout: int = 30
    readonly: bool = False


# Hardcoded sector map — legacy fallback. The SectorResolver in
# core/sector_resolver.py fetches sectors from yfinance and caches them
# in Supabase. This map is only used as a seed / override.
DEFAULT_SECTOR_MAP: Dict[str, str] = {}


@dataclass
class RiskConfig:
    # Position-level limits
    max_risk_per_trade: float = 0.01
    min_position_size: float = 100.0

    # Portfolio-level limits
    max_total_exposure: float = 0.80
    max_single_position: float = 0.15
    max_sector_exposure: float = 0.30
    max_concurrent_positions: int = 5
    max_daily_trades: int = 20
    max_leverage: float = 1.0

    # Circuit breakers
    daily_loss_half_size: float = 0.02
    daily_loss_close_all: float = 0.03
    weekly_loss_half_size: float = 0.05
    weekly_loss_close_all: float = 0.07
    peak_drawdown_stop: float = 0.10
    lock_file_path: str = "smart_trader/TRADING_HALTED.lock"

    # Correlation checks
    correlation_window: int = 60
    correlation_reduce_threshold: float = 0.70
    correlation_reject_threshold: float = 0.85

    # Gap risk
    gap_risk_multiplier: float = 3.0
    gap_risk_max_loss: float = 0.02

    # Order validation
    max_bid_ask_spread: float = 0.005
    duplicate_order_window_sec: int = 60

    # Sector mapping
    sector_map: Dict[str, str] = field(default_factory=lambda: DEFAULT_SECTOR_MAP.copy())


@dataclass
class SmartMoneyConfig:
    """Multi-source smart money stock selection scanner."""
    smart_money_enabled: bool = True

    # Per-provider toggles — existing scanner providers
    capitol_trades_enabled: bool = True
    sec_edgar_enabled: bool = True
    berkshire_enabled: bool = True
    ark_enabled: bool = True
    insider_cluster_enabled: bool = True

    # Per-provider toggles — new Smart_Money_Portfolio holdings providers
    pershing_square_enabled: bool = True
    # Appaloosa converted to a family office in 2019 and stopped filing 13Fs;
    # its most recent SEC filing is from 2016 and would give stale data.
    # Flip this to True only if you explicitly want a static 2016 snapshot.
    appaloosa_enabled: bool = False
    duquesne_enabled: bool = True

    # Tier-A concentrated long-only 13F filers (added 2026-04)
    tci_enabled: bool = True
    baupost_enabled: bool = True
    akre_enabled: bool = True
    viking_enabled: bool = True
    altimeter_enabled: bool = True
    third_point_enabled: bool = True
    lone_pine_enabled: bool = True
    # Greenlight's most recent 13F is from Feb 2024; AUM has dropped below
    # the $100M 13F filing threshold, so the filing has effectively stopped.
    # Same pattern as Appaloosa. Flip to True if a new filing surfaces.
    greenlight_enabled: bool = False

    # Tier-B active transparent ETFs (added 2026-04)
    moat_enabled: bool = True
    cgdv_enabled: bool = True
    syld_enabled: bool = True

    # Trade filters
    min_trade_amount: float = 50_000.0
    min_share_change: int = 10_000
    recency_window_days: int = 30
    berkshire_recency_days: int = 90

    # Cache TTLs (hours)
    capitol_trades_cache_ttl: float = 24.0
    sec_edgar_cache_ttl: float = 12.0
    berkshire_cache_ttl: float = 168.0
    ark_cache_ttl: float = 24.0
    insider_cluster_cache_ttl: float = 24.0
    pershing_square_cache_ttl: float = 168.0
    appaloosa_cache_ttl: float = 168.0
    duquesne_cache_ttl: float = 168.0
    # Tier-A 13F — weekly cache (filings are quarterly)
    tci_cache_ttl: float = 168.0
    baupost_cache_ttl: float = 168.0
    akre_cache_ttl: float = 168.0
    viking_cache_ttl: float = 168.0
    altimeter_cache_ttl: float = 168.0
    third_point_cache_ttl: float = 168.0
    lone_pine_cache_ttl: float = 168.0
    greenlight_cache_ttl: float = 168.0
    # Tier-B ETF — daily cache (holdings publish daily)
    moat_cache_ttl: float = 24.0
    cgdv_cache_ttl: float = 24.0
    syld_cache_ttl: float = 24.0

    # Politician ranking
    top_n_politicians: int = 10
    min_politician_filings: int = 2

    # ARK filtering
    min_ark_buying_days: int = 3

    # Insider cluster detection
    cluster_window_days: int = 30
    min_cluster_insiders: int = 2

    # ------------------------------------------------------------------
    # Conviction scoring (agreement-driven model)
    #
    # conviction = Σ source_quality(distinct sources)
    #            + cluster_bonus(distinct actors agreeing)
    #            + dollar_bonus(tamed, capped modifier)
    #            + accumulation_bonus(repeated filings)
    #            + recency_bonus(linear decay)
    #
    # Rationale: multiple independent credible actors agreeing on a name is a
    # stronger signal than one large disclosed dollar figure, which for
    # Capitol Trades is just the midpoint of a wide disclosed range. Dollars
    # are a capped modifier, not the driver. See _compute_conviction_score().
    # ------------------------------------------------------------------
    # Per-source quality weight, summed across the DISTINCT sources that
    # contributed a qualifying buy for a symbol. Unlisted sources fall back to
    # conviction_default_source_weight. Reflects documented signal reliability:
    # corporate insiders > congress / 13F > high-turnover momentum ETFs.
    conviction_source_weights: Dict[str, float] = field(default_factory=lambda: {
        "insider_cluster": 3.5,   # clustered corporate insiders — strongest signal
        "sec_edgar": 3.0,         # individual Form 4 insider buy
        "capitol_trades": 2.5,    # congressional — real alpha but noisy / lagged
        "berkshire_13f": 2.5,     # quarterly 13F (45-day delayed)
        "ark_invest": 1.5,        # high-turnover momentum ETF
    })
    conviction_default_source_weight: float = 2.0

    # Cluster: reward independent ACTORS agreeing on the same name.
    #   bonus = min(cap, per_actor * (n_actors - 1))
    conviction_cluster_per_actor: float = 1.0
    conviction_cluster_cap: float = 3.0

    # Dollars as a tamed modifier. Only dollars above 10^floor_log contribute,
    # so one large disclosed figure can't dominate genuine multi-actor agreement.
    #   bonus = min(cap, coef * max(0, log10($+1) - floor_log))
    conviction_dollar_coef: float = 0.5
    conviction_dollar_floor_log: float = 5.0   # $100k
    conviction_dollar_cap: float = 2.0

    # Accumulation: repeated qualifying filings = building a position.
    #   bonus = min(cap, per_filing * (filing_count - 1))
    conviction_accum_per_filing: float = 0.3
    conviction_accum_cap: float = 1.5

    # Recency: linear decay over this many days.
    conviction_recency_days: float = 30.0

    # Defensive symbols (retained for future VIX/market-filter gating; scanner
    # in smart-trader is called with vol_rank=0.0 so this list is currently
    # not used to filter).
    defensive_symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "JNJ", "PG", "KO", "PEP", "WMT", "UNH", "V",
    ])

    # Disk cache
    disk_cache_dir: str = "smart_trader/cache/smart_money"

    # Provider health monitoring — warn when a scanner provider has not
    # produced a non-empty fetch within these many days. Catches silent
    # parser/upstream regressions like the 2026-04 Capitol Trades / Insider
    # Cluster / Berkshire 13F outages, where each provider returned 0 filings
    # for days without raising. Thresholds reflect each provider's natural
    # cadence: Berkshire is quarterly, the others have daily/sub-weekly
    # signal flow. Providers absent from this dict are not health-checked.
    provider_health_max_empty_days: Dict[str, float] = field(default_factory=lambda: {
        "capitol_trades": 3.0,
        "sec_edgar": 2.0,
        "berkshire_13f": 100.0,
        "ark_invest": 3.0,
        "insider_cluster": 3.0,
    })

    # Supabase (credentials from .env)
    supabase_enabled: bool = True

    # --------------------------------------------------------------------
    # Smart_Money_Portfolio feature
    # --------------------------------------------------------------------
    # top_n_size tuned up from the original 10 after reviewing Path B trigger
    # frequency on a 136-ticker universe: 10 was ~7% of the universe, so ~0.2
    # expected scanner-overlap intersections per cycle. 30 (~22%) gives ~0.7
    # expected intersections — roughly 3x more Path B / Overlap triggers while
    # the top-10 conviction picks are still the highest-scored entries.
    top_n_size: int = 30
    portfolio_retention_days: int = 90
    portfolio_filter_enabled: bool = True
    performance_lookback_days: int = 180

    # Composite_Score weights (must sum to 1.0). Tuned after empirical review
    # of the first live 136-ticker universe: overlap-heavy weighting surfaces
    # cross-fund conviction picks (AMZN/GOOGL/TSM/QSR + concentrated single-
    # fund bets like AAPL at 22% of Berkshire) rather than letting ARK's
    # 173 small thematic positions flood the top-N via momentum alone.
    overlap_weight: float = 0.60
    holding_weight_weight: float = 0.30
    performance_weight: float = 0.10
    momentum_weight: float = 0.00
    relative_strength_weight: float = 0.00

    # Entry_Calculator
    entry_vwap_lookback_days: int = 20
    entry_support_lookback_days: int = 20
    entry_price_bypass_enabled: bool = True

    # Path A fires when current_price <= optimal_entry_price * (1 + tolerance).
    # 0.05 = 5% — trades within arm's reach of the 20-day low/VWAP instead of
    # waiting for an exact touch. Tightens to pure "at or below" behavior when
    # set to 0.0. Applies to both Path A and the overlap path.
    entry_price_tolerance: float = 0.05

    # -----------------------------------------------------------------------
    # Technical primary strategy (Humbled Trader — 200 SMA + 8 EMA + gap up)
    # Technical signals are the PRIMARY entry gate; smart money data becomes a
    # confidence scoring add-on that determines position size.
    # -----------------------------------------------------------------------
    trend_filter_enabled: bool = True
    trend_sma_period: int = 200         # long-term trend SMA; only long above this

    ema_pullback_enabled: bool = True
    ema_period: int = 8                 # momentum EMA; enter on dips to this line
    ema_tolerance: float = 0.02         # fire up to 2% above the 8 EMA

    gap_up_enabled: bool = True
    gap_up_threshold: float = 0.03      # 3% minimum overnight gap to trigger

    # Smart money confidence bonus thresholds (added to base technical confidence)
    sm_bonus_low_conviction: float = 3.0    # scanner conviction floor for +0.15 bonus
    sm_bonus_high_conviction: float = 6.0   # scanner conviction floor for +0.25 bonus
    sm_bonus_overlap_multi: float = 0.20    # bonus when overlap_count >= 2 funds
    sm_bonus_overlap_single: float = 0.10   # bonus when overlap_count == 1 fund

    # Risk multiplier tiers applied via Signal.metadata["risk_multiplier"]
    sm_risk_mult_low: float = 0.75          # confidence < sm_risk_thresh_low
    sm_risk_thresh_low: float = 0.60
    sm_risk_mult_mid: float = 1.0           # sm_risk_thresh_low <= conf < sm_risk_thresh_high
    sm_risk_thresh_high: float = 0.80
    sm_risk_mult_high: float = 1.5          # confidence >= sm_risk_thresh_high

    # When False (default), smart-money scanner candidates NEVER trigger a
    # standalone entry (the legacy "Path B"). Conviction then acts ONLY as a
    # supplement to the technical swing setup — via the confidence bonus and
    # risk multiplier above — so every entry requires a 200-SMA + 8-EMA/gap-up
    # trigger. Set True to re-enable independent scanner entries.
    scanner_standalone_entries_enabled: bool = False

    # -----------------------------------------------------------------------
    # Market regime gate (smart_trader.core.regime). Blocks NEW entries when
    # the regime proxy (SPY) is unfavorable; existing positions and exits are
    # never gated. Posture "defensive" (default) == the strict "above 50 AND
    # 200 SMA" gate validated in backtests (smoothest drawdowns). "aggressive"
    # also trades the ambiguous below-200 zone (captures recoveries off a
    # bottom, but eats bear-market-rally losses). Set enabled=False to disable.
    # -----------------------------------------------------------------------
    regime_filter_enabled: bool = True
    regime_posture: str = "defensive"      # "defensive" | "aggressive"
    regime_symbol: str = "SPY"
    regime_slope_lookback: int = 20

    # -----------------------------------------------------------------------
    # Catalyst analyzer — news/event validation for all technical signals.
    # Runs for both gap up and EMA pullback signals. Gap ups get a stronger
    # penalty for missing catalysts (ghost-gap guard); pullbacks are not
    # penalized for no-news since technical retracements are valid without news.
    # -----------------------------------------------------------------------
    catalyst_enabled: bool = True
    catalyst_cache_dir: str = "smart_trader/cache/catalyst"
    catalyst_cache_ttl_hours: float = 6.0       # news cache refresh cadence
    catalyst_news_lookback_hours: int = 48       # discard news older than this

    # Gap up adjustments (stronger — gaps need catalyst validation)
    catalyst_gap_boost_strong: float = 0.20     # earnings beat / acquisition / guidance raise
    catalyst_gap_boost_moderate: float = 0.10  # upgrade / product launch / buyback / partnership
    catalyst_gap_penalty_no_news: float = 0.05 # ghost gap penalty (not applied to pullbacks)
    catalyst_gap_penalty_negative: float = 0.20 # earnings miss / downgrade / lawsuit

    # EMA pullback adjustments (softer — pullbacks are valid without news)
    catalyst_pullback_boost_strong: float = 0.15
    catalyst_pullback_boost_moderate: float = 0.08
    catalyst_pullback_penalty_negative: float = 0.15

    # Tradability gate applied in main._current_top_n() before signals are
    # generated. A top-N stock only triggers Path A / B / Overlap if EITHER
    #   (a) it's held by >= min_overlap_count_for_trading distinct funds, OR
    #   (b) a single fund holds it at >= single_fund_concentration_exception
    #       of its book (e.g. AAPL at 22.6% of Berkshire passes even with
    #       overlap_count=1).
    # Default (2, 0.15) filters out low-conviction single-fund picks like
    # ESLT (ARK 1.6%) while keeping legit concentrated bets. Set
    # min_overlap_count_for_trading=1 to disable the rule entirely.
    min_overlap_count_for_trading: int = 2
    single_fund_concentration_exception: float = 0.15

    # Portfolio DB path (legacy — Supabase is now primary)
    portfolio_db_path: str = ""

    def __post_init__(self) -> None:
        weight_sum = (
            self.overlap_weight
            + self.holding_weight_weight
            + self.performance_weight
            + self.momentum_weight
            + self.relative_strength_weight
        )
        if abs(weight_sum - 1.0) > 1e-6:
            raise ValueError(
                f"scoring weights must sum to 1.0, got {weight_sum:.6f} "
                f"(overlap={self.overlap_weight}, holding={self.holding_weight_weight}, "
                f"performance={self.performance_weight}, momentum={self.momentum_weight}, "
                f"rs={self.relative_strength_weight})"
            )


@dataclass
class LadderInConfig:
    """Dollar-cost averaging on dips for held positions."""
    ladder_in_enabled: bool = True

    # Fixed thresholds — no regime gating in smart-trader. Each held position
    # that drops past a threshold from its entry price gets a fixed-share add.
    thresholds: List[float] = field(default_factory=lambda: [-0.15, -0.25])
    shares: List[int] = field(default_factory=lambda: [10, 20])


@dataclass
class MonitoringConfig:
    log_level: str = "INFO"
    log_dir: str = "logs"
    trade_log_file: str = "trades.log"
    state_snapshot_path: str = "smart_trader/state_snapshot.json"
    max_log_size_mb: int = 10
    log_retention_days: int = 30
    alert_cooldown_seconds: int = 300
    enable_email_alerts: bool = False
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_from: str = ""
    email_to: str = ""
    enable_webhook_alerts: bool = False
    webhook_url: str = ""
    api_port: int = 8000
    api_cors_origins: List[str] = field(default_factory=lambda: [
        "http://localhost:3000",
        "http://localhost:5173",
    ])


@dataclass
class DataConfig:
    """Data persistence config. Supabase is the primary store."""
    # Legacy field kept for backward compat — no longer used.
    db_path: str = ""
    supabase_sync_enabled: bool = True


@dataclass
class ExitConfig:
    """Signal-driven exits — sources trusted to enter on can also trigger exit.

    All three triggers (smart-money-sell, top-N drop-out, conviction-decay)
    are LIVE by default after the Phase 2/3/4 rollout. Set the relevant
    dry-run flag back to True to put a single trigger into preview mode
    (e.g. for debugging or after a config change).
    """
    enabled: bool = True
    # Global dry-run kill switch. When True, ALL three triggers stay in
    # preview regardless of per-trigger flags. Use this to roll back the
    # whole feature without flipping each trigger individually.
    dry_run: bool = False

    # Trigger 1: smart-money explicit SELL/DECREASE filings on held
    # positions. Effective dry-run = self.dry_run.
    on_smart_money_sell: bool = True

    # Trigger 2: held position has fallen out of the top-N portfolio
    # for `top_n_dropout_days_required` consecutive days. Hysteresis is
    # mandatory — top-N flicker is normal and one bad day shouldn't unwind a
    # position. Held symbols never observed in top-N (manual holds,
    # pre-bot positions) are exempt: drop-out only fires for symbols the
    # system saw in top-N and then saw drop out.
    on_top_n_dropout: bool = True
    top_n_dropout_days_required: int = 3
    # Per-trigger dry-run override. Effective dry-run for a dropout exit is
    # (global dry_run OR top_n_dropout_dry_run). Flip back to True to put
    # this trigger into preview without affecting the others.
    top_n_dropout_dry_run: bool = False

    # Trigger 3: held symbol's current buy-side conviction has been
    # below `min_held_conviction_score` for `conviction_decay_days_required`
    # consecutive observed cycles. Eligibility is gated on prior top-N
    # observation (manual holds and pre-bot positions don't trigger).
    # Skipped when scanner data is unavailable for the cycle (avoids
    # treating an outage as a portfolio-wide decay).
    on_conviction_decay: bool = True
    conviction_decay_days_required: int = 3
    # Looser than entry's `min_conviction_score` (default 3.0) — once a
    # position is open, it doesn't need to keep clearing the entry bar
    # every day; we only exit when conviction has clearly faded.
    min_held_conviction_score: float = 2.0
    # Per-trigger dry-run override, same pattern as top_n_dropout_dry_run.
    conviction_decay_dry_run: bool = False

    # Conviction floor for an exit signal. Set lower than entry's
    # min_conviction_score (3.0) — a single explicit sell on a held position
    # is meaningful even at modest dollar volume. 0.0 disables the floor.
    # Applies to the smart-money-sell trigger only; drop-out is structural,
    # not conviction-based.
    min_exit_conviction_score: float = 2.0

    # Whipsaw guards (apply in BOTH dry-run and live so dry-run preview
    # matches live behavior).
    #
    # Min holding period: a position must have been held at least this many
    # days before a signal-driven exit can fire. Smart-money sources oscillate
    # quarterly; without this the bot can buy on a stale BUY filing and exit
    # the next cycle on a fresh SELL. Set to 0 to disable.
    min_holding_period_days: int = 14
    # Re-entry cooldown: after a signal-driven exit, suppress new entries on
    # the same symbol for this many days. Prevents same-cycle re-entry when a
    # SELL filing and a BUY filing both surface for one symbol. Set to 0 to
    # disable.
    reentry_cooldown_days: int = 7


@dataclass
class TraderConfig:
    """Orchestrator-level settings for the trading loop."""
    # Symbols always monitored for ladder-in / already-held positions. Can be
    # empty — the scanner will fill the candidate set dynamically.
    watchlist: List[str] = field(default_factory=list)

    cycle_interval_seconds: int = 3600  # 1 hour

    # Skip the cycle's scan/AI work when the US market is closed (nights,
    # weekends, holidays) to cut idle API/credit usage. See core.market_hours.
    market_hours_gate_enabled: bool = True

    # Entry signal defaults (applied when building Signals from scanner candidates)
    default_stop_pct: float = 0.08      # 8% hard stop below entry
    default_trail_pct: float = 0.05     # 5% trailing stop
    default_take_profit_pct: float = 0.20  # 20% take-profit

    # Churn controls
    # Loosened from 5.0 after seeing that very few scanner candidates passed
    # the old bar on typical cycles. 3.0 still requires a scanner source plus
    # at least recent activity or non-zero dollar volume — not total noise.
    # Raising this back up is the lever for fewer-but-higher-quality Path B /
    # Overlap trades.
    min_conviction_score: float = 3.0          # Scanner candidates below this are skipped
    max_new_positions_per_cycle: int = 2       # Prevent over-allocation on single cycle

    # HMM training is gone — smart_money scanner is called with this fixed value.
    # Reserve the hook so a future VIX/SMA-200 gate can compute this dynamically.
    scanner_vol_rank: float = 0.0


@dataclass
class QwenAgentConfig:
    """Configuration for the Qwen Cloud AI agent layer."""
    # Master switch
    qwen_enabled: bool = True

    # Per-component toggles
    catalyst_classification_enabled: bool = True
    signal_arbitration_enabled: bool = True
    commentary_enabled: bool = True

    # Model settings
    model_name: str = "qwen-plus"
    api_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    api_timeout_seconds: int = 10       # range [1, 120]
    max_retries: int = 2                # range [0, 10]

    # Commentary
    max_commentary_chars: int = 2000
    max_commentary_tokens: int = 500    # range [50, 2000]
    commentary_timeout_seconds: int = 30

    # Arbitration
    max_reasoning_chars: int = 500

    def __post_init__(self) -> None:
        if not (1 <= self.api_timeout_seconds <= 120):
            raise ValueError(
                f"api_timeout_seconds must be in range [1, 120], got {self.api_timeout_seconds}"
            )
        if not (0 <= self.max_retries <= 10):
            raise ValueError(
                f"max_retries must be in range [0, 10], got {self.max_retries}"
            )
        if not (50 <= self.max_commentary_tokens <= 2000):
            raise ValueError(
                f"max_commentary_tokens must be in range [50, 2000], got {self.max_commentary_tokens}"
            )


def create_demo_smart_money_config() -> SmartMoneyConfig:
    """Return a SmartMoneyConfig with only 4 demo providers enabled.

    Used when the system falls back to mock broker mode to minimize external
    network dependencies. Only sec_edgar, ark, berkshire, and moat are enabled.
    """
    return SmartMoneyConfig(
        # Keep the scanner enabled
        smart_money_enabled=True,
        # Only these 4 providers are active in demo mode
        sec_edgar_enabled=True,
        ark_enabled=True,
        berkshire_enabled=True,
        moat_enabled=True,
        # All other providers disabled
        capitol_trades_enabled=False,
        insider_cluster_enabled=False,
        pershing_square_enabled=False,
        appaloosa_enabled=False,
        duquesne_enabled=False,
        tci_enabled=False,
        baupost_enabled=False,
        akre_enabled=False,
        viking_enabled=False,
        altimeter_enabled=False,
        third_point_enabled=False,
        lone_pine_enabled=False,
        greenlight_enabled=False,
        cgdv_enabled=False,
        syld_enabled=False,
    )


@dataclass
class AppConfig:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    smart_money: SmartMoneyConfig = field(default_factory=SmartMoneyConfig)
    ladder_in: LadderInConfig = field(default_factory=LadderInConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    data: DataConfig = field(default_factory=DataConfig)
    trader: TraderConfig = field(default_factory=TraderConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    qwen_agent: QwenAgentConfig = field(default_factory=QwenAgentConfig)
    demo_mode: bool = False


def load_config() -> AppConfig:
    return AppConfig()
