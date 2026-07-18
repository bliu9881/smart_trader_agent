"""Demo / sample data for populating the dashboard when the live pipeline is idle.

Enabled by the ``DEMO_SEED`` environment variable (``1``/``true``/``yes``/``on``).
When on, :func:`demo_overlay` fills ONLY the dashboard sections that are currently
empty — real trading/scanner data always wins the moment it appears, so this is
safe to leave enabled and it simply "gets out of the way" once the real pipeline
produces output.

Numbers are internally consistent (equity = cash + Σ market value; daily P&L and
allocation are fractions, matching the frontend's ``n * 100`` formatting) so the
dashboard reads like a real, coherent snapshot rather than random values.

This module never touches broker/order logic — it only shapes read-model state
served by the API. All values are clearly synthetic sample data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def _is_on(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Section builders — each matches the shape the API/frontend expects.
# ---------------------------------------------------------------------------

def demo_positions() -> List[Dict[str, Any]]:
    """Open positions (see api.ts PositionData)."""
    return [
        {"symbol": "NVDA", "quantity": 120, "avg_cost": 118.50, "market_price": 132.40,
         "market_value": 15888.00, "unrealized_pnl": 1668.00, "stop_price": 125.00},
        {"symbol": "AAPL", "quantity": 80, "avg_cost": 210.20, "market_price": 224.65,
         "market_value": 17972.00, "unrealized_pnl": 1156.00, "stop_price": 214.00},
        {"symbol": "MSFT", "quantity": 40, "avg_cost": 415.00, "market_price": 408.30,
         "market_value": 16332.00, "unrealized_pnl": -268.00, "stop_price": 399.00},
    ]


# Derived portfolio math (kept consistent with demo_positions()):
#   cost basis = 47,636 → cash = 100,000 - 47,636 = 52,364
#   market value = 50,192 → equity = 52,364 + 50,192 = 102,556
#   unrealized = +2,556; assume +1,284 of it is today's move.
_DEMO_CASH = 52364.00
_DEMO_MARKET_VALUE = 50192.00
_DEMO_EQUITY = _DEMO_CASH + _DEMO_MARKET_VALUE          # 102,556
_DEMO_DAILY_PNL = 1284.00
_DEMO_PEAK_EQUITY = 103200.00


def demo_portfolio_fields() -> Dict[str, Any]:
    """Portfolio overview fields (see api.ts PortfolioData). Percents are fractions."""
    prev_equity = _DEMO_EQUITY - _DEMO_DAILY_PNL
    return {
        "equity": _DEMO_EQUITY,
        "cash": _DEMO_CASH,
        "buying_power": _DEMO_CASH,
        "daily_pnl": _DEMO_DAILY_PNL,
        "daily_pnl_pct": round(_DEMO_DAILY_PNL / prev_equity, 4),      # ~0.0127
        "allocation_pct": round(_DEMO_MARKET_VALUE / _DEMO_EQUITY, 4),  # ~0.4894
        "leverage": 1.0,
    }


def demo_risk_status() -> Dict[str, Any]:
    """Risk panel (see api.ts RiskData). Mirrors the real circuit-breaker map."""
    return {
        "daily_pnl": _DEMO_DAILY_PNL,
        "weekly_pnl": 2980.00,
        "peak_drawdown": 0.021,
        "status": "ok",
        "is_half_size": False,
        "is_closed": False,
        "is_halted": False,
        "peak_equity": _DEMO_PEAK_EQUITY,
        "current_equity": _DEMO_EQUITY,
        "circuit_breakers": {
            "daily_half_size": "2%",
            "daily_close_all": "3%",
            "weekly_half_size": "5%",
            "weekly_close_all": "7%",
            "peak_drawdown_stop": "10%",
        },
    }


def demo_signals() -> List[Dict[str, Any]]:
    """Recent signal feed with AI reasoning (see api.ts SignalData)."""
    now = datetime.now()

    def ts(mins_ago: int) -> str:
        return (now - timedelta(minutes=mins_ago)).isoformat()

    return [
        {
            "timestamp": ts(3), "symbol": "NVDA", "direction": "LONG",
            "strategy": "EmaPullback", "size_pct": "8%", "shares": 60,
            "entry_price": 131.90, "stop_loss": 125.00, "status": "approved",
            "reasoning": "8-EMA pullback in an uptrend above the 200-SMA; earnings-beat catalyst confirmed.",
            "rejection_reason": "", "modifications": ["Trimmed size to sector cap"],
            "is_ladder_in": False, "is_smart_money": True,
            "smart_money_conviction": 8.4, "smart_money_sources": ["SEC Form 4", "ARK Invest"],
            "arbitration_reasoning": "Ranked #1 — strongest catalyst and multi-source smart-money agreement.",
        },
        {
            "timestamp": ts(3), "symbol": "AAPL", "direction": "LONG",
            "strategy": "GapUp", "size_pct": "6%", "shares": 45,
            "entry_price": 224.00, "stop_loss": 214.00, "status": "approved",
            "reasoning": "Gap-up on product-launch catalyst; volume 2.3x average.",
            "rejection_reason": "", "modifications": [],
            "is_ladder_in": False, "is_smart_money": True,
            "smart_money_conviction": 7.1, "smart_money_sources": ["Berkshire 13F"],
            "arbitration_reasoning": "Ranked #2 — solid catalyst, single-source conviction.",
        },
        {
            "timestamp": ts(8), "symbol": "TSLA", "direction": "LONG",
            "strategy": "EmaPullback", "size_pct": "0%", "shares": 0,
            "entry_price": 242.10, "stop_loss": 228.00, "status": "rejected",
            "reasoning": "8-EMA pullback with ARK accumulation.",
            "rejection_reason": "Sector cap reached — Consumer Discretionary at 25%.",
            "modifications": [], "is_ladder_in": False, "is_smart_money": True,
            "smart_money_conviction": 6.2, "smart_money_sources": ["ARK Invest"],
        },
        {
            "timestamp": ts(8), "symbol": "AMD", "direction": "LONG",
            "strategy": "GapUp", "size_pct": "0%", "shares": 0,
            "entry_price": 168.30, "stop_loss": 159.00, "status": "rejected",
            "reasoning": "Gap-up of 4.1% at the open.",
            "rejection_reason": "No catalyst found — ghost-gap guard blocked entry.",
            "modifications": [], "is_ladder_in": False, "is_smart_money": False,
            "smart_money_conviction": 4.8, "smart_money_sources": [],
        },
        {
            "timestamp": ts(3), "symbol": "MSFT", "direction": "LONG",
            "strategy": "EmaPullback", "size_pct": "5%", "shares": 40,
            "entry_price": 405.00, "stop_loss": 399.00, "status": "pending",
            "reasoning": "Awaiting entry trigger at the 8-EMA (405.00); wide-moat holding.",
            "rejection_reason": "", "modifications": [],
            "is_ladder_in": True, "is_smart_money": True,
            "smart_money_conviction": 6.9, "smart_money_sources": ["Morningstar Wide Moat ETF"],
        },
    ]


def demo_smart_money() -> List[Dict[str, Any]]:
    """Smart-money scanner candidates (see api.ts SmartMoneyCandidate)."""
    now = datetime.now(tz=timezone.utc)

    def filed(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).isoformat()

    return [
        {"symbol": "NVDA", "conviction_score": 8.4, "sources": ["SEC Form 4", "ARK Invest"],
         "actors": ["Jensen Huang", "Cathie Wood"], "total_dollar_volume": 42_500_000.0,
         "filing_count": 5, "most_recent_filing": filed(1)},
        {"symbol": "AAPL", "conviction_score": 7.1, "sources": ["Berkshire 13F"],
         "actors": ["Warren Buffett"], "total_dollar_volume": 128_000_000.0,
         "filing_count": 3, "most_recent_filing": filed(2)},
        {"symbol": "MSFT", "conviction_score": 6.9, "sources": ["Morningstar Wide Moat ETF"],
         "actors": ["Index Reconstitution"], "total_dollar_volume": 58_000_000.0,
         "filing_count": 2, "most_recent_filing": filed(3)},
        {"symbol": "AVGO", "conviction_score": 6.5, "sources": ["SEC Form 4"],
         "actors": ["Hock Tan"], "total_dollar_volume": 22_000_000.0,
         "filing_count": 3, "most_recent_filing": filed(2)},
        {"symbol": "TSLA", "conviction_score": 6.2, "sources": ["ARK Invest"],
         "actors": ["Cathie Wood"], "total_dollar_volume": 31_000_000.0,
         "filing_count": 4, "most_recent_filing": filed(1)},
        {"symbol": "LLY", "conviction_score": 5.9, "sources": ["Berkshire 13F"],
         "actors": ["Institutional 13F"], "total_dollar_volume": 40_000_000.0,
         "filing_count": 2, "most_recent_filing": filed(4)},
    ]


def demo_regime() -> Dict[str, Any]:
    """Market-regime snapshot (see api.ts RegimeData)."""
    return {
        "zone": "bull",
        "posture": "defensive",
        "entries_allowed": True,
        "close": 563.20,
        "sma_50": 548.90,
        "sma_200": 521.30,
        "above_50": True,
        "above_200": True,
        "sma200_rising": True,
        "reason": "SPY above a rising 200-SMA — bull zone, entries enabled.",
    }


def demo_alerts() -> List[Dict[str, Any]]:
    """Recent alert history (see api.ts AlertData)."""
    now = datetime.now(tz=timezone.utc)

    def ts(mins_ago: int) -> str:
        return (now - timedelta(minutes=mins_ago)).isoformat()

    return [
        {"timestamp": ts(3), "trigger": "position_opened", "severity": "info",
         "subject": "Entry: NVDA", "message": "Opened 60 sh NVDA @ $131.90 (8-EMA pullback)."},
        {"timestamp": ts(3), "trigger": "catalyst_detected", "severity": "info",
         "subject": "Catalyst: AAPL", "message": "Product-launch catalyst detected — sentiment +0.8."},
        {"timestamp": ts(8), "trigger": "risk_check", "severity": "warning",
         "subject": "Sector cap", "message": "TSLA entry blocked — Consumer Discretionary at 25% cap."},
        {"timestamp": ts(12), "trigger": "circuit_breaker", "severity": "info",
         "subject": "All clear", "message": "Daily drawdown 0.0% — well within the 3% close-all limit."},
    ]


# ---------------------------------------------------------------------------
# Overlay — fill ONLY empty sections so real data always wins.
# ---------------------------------------------------------------------------

def demo_overlay(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return an api_state update dict that fills only the empty dashboard sections.

    ``snapshot`` is the current store snapshot. A section is considered empty when
    its list is falsy (positions/signals/candidates/alerts) or ``regime`` is None.
    When positions are empty we also seed the coherent portfolio + risk figures so
    the numbers match the demo holdings.
    """
    updates: Dict[str, Any] = {}

    if not snapshot.get("positions"):
        updates["positions"] = demo_positions()
        updates.update(demo_portfolio_fields())
        updates["risk_status"] = demo_risk_status()

    if not snapshot.get("recent_signals"):
        updates["recent_signals"] = demo_signals()

    if not snapshot.get("smart_money_candidates"):
        updates["smart_money_candidates"] = demo_smart_money()

    if snapshot.get("regime") is None:
        updates["regime"] = demo_regime()

    if not snapshot.get("alert_history"):
        updates["alert_history"] = demo_alerts()

    return updates
