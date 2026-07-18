"""North-America (US equities / NYSE) market-hours helper.

Used to gate the trading loop so it doesn't scan or make Qwen calls when the
US market is closed (nights, weekends, holidays) — cutting idle API/credit use.

Regular trading hours: 09:30–16:00 America/New_York, Mon–Fri, excluding NYSE
holidays. Early-close half-days (e.g. day after Thanksgiving) are treated as
normal sessions — the gate is a cost optimization, not an execution-critical
calendar, so being open a few extra hours on a half-day is harmless.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# NYSE full-day holidays (observed dates). Covers 2025–2027; a missing/stale
# year just means the gate runs on a closed day — a wasted cheap cycle, never
# a wrong trade (the broker/regime paths are unaffected).
_NYSE_HOLIDAYS: frozenset[str] = frozenset({
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def is_market_open(now_et: datetime | None = None) -> bool:
    """Return True if the US equities market is in a regular session right now."""
    now = now_et or datetime.now(_ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if now.date().isoformat() in _NYSE_HOLIDAYS:
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def market_status_reason(now_et: datetime | None = None) -> str:
    """Short human-readable reason describing the current market state."""
    now = now_et or datetime.now(_ET)
    stamp = now.strftime("%a %H:%M ET")
    if now.weekday() >= 5:
        return f"weekend, {stamp}"
    if now.date().isoformat() in _NYSE_HOLIDAYS:
        return f"NYSE holiday, {stamp}"
    if now.time() < _MARKET_OPEN:
        return f"pre-market, {stamp}"
    if now.time() > _MARKET_CLOSE:
        return f"after-hours, {stamp}"
    return f"open, {stamp}"
