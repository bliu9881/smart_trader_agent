"""Tests for ProviderHealthTracker."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from smart_trader.core.smart_money_providers._provider_health import (
    ProviderHealth,
    ProviderHealthTracker,
)


def _tracker(tmp_path: Path, **thresholds) -> ProviderHealthTracker:
    defaults = {"capitol_trades": 3.0, "sec_edgar": 2.0}
    defaults.update(thresholds)
    return ProviderHealthTracker(cache_dir=tmp_path, max_empty_days=defaults)


# ---------------------------------------------------------------------------
# record_fetch — counter / timestamp logic
# ---------------------------------------------------------------------------

def test_first_non_empty_fetch_initializes_state(tmp_path):
    t = _tracker(tmp_path)
    t.record_fetch("capitol_trades", 5)
    h = t.state["capitol_trades"]
    assert h.last_fetch_count == 5
    assert h.last_non_empty_count == 5
    assert h.consecutive_empty_fetches == 0
    assert h.last_fetch_at is not None
    assert h.last_non_empty_at == h.last_fetch_at


def test_first_empty_fetch_records_streak_one(tmp_path):
    t = _tracker(tmp_path)
    t.record_fetch("capitol_trades", 0)
    h = t.state["capitol_trades"]
    assert h.consecutive_empty_fetches == 1
    assert h.last_fetch_at is not None
    assert h.last_non_empty_at is None  # never returned data


def test_consecutive_empty_fetches_increments(tmp_path):
    t = _tracker(tmp_path)
    t.record_fetch("capitol_trades", 7)
    t.record_fetch("capitol_trades", 0)
    t.record_fetch("capitol_trades", 0)
    t.record_fetch("capitol_trades", 0)
    h = t.state["capitol_trades"]
    assert h.consecutive_empty_fetches == 3
    assert h.last_non_empty_count == 7  # preserved across empty streak


def test_non_empty_fetch_resets_streak(tmp_path):
    t = _tracker(tmp_path)
    t.record_fetch("capitol_trades", 0)
    t.record_fetch("capitol_trades", 0)
    t.record_fetch("capitol_trades", 12)
    h = t.state["capitol_trades"]
    assert h.consecutive_empty_fetches == 0
    assert h.last_non_empty_count == 12


# ---------------------------------------------------------------------------
# degraded_providers — threshold logic
# ---------------------------------------------------------------------------

def test_recent_non_empty_not_degraded(tmp_path):
    t = _tracker(tmp_path)
    t.record_fetch("capitol_trades", 10)
    assert t.degraded_providers() == []


def test_silent_past_threshold_is_degraded(tmp_path):
    t = _tracker(tmp_path, capitol_trades=3.0)
    t.record_fetch("capitol_trades", 10)
    # Backdate the last non-empty into the past
    t.state["capitol_trades"].last_non_empty_at = datetime.now() - timedelta(days=4)
    degraded = t.degraded_providers()
    assert [n for n, _ in degraded] == ["capitol_trades"]
    assert "silent for 4" in degraded[0][1]
    assert "10 filings" in degraded[0][1]


def test_silent_within_threshold_not_degraded(tmp_path):
    t = _tracker(tmp_path, capitol_trades=3.0)
    t.record_fetch("capitol_trades", 10)
    t.state["capitol_trades"].last_non_empty_at = datetime.now() - timedelta(days=2)
    assert t.degraded_providers() == []


def test_never_returned_past_threshold_is_degraded(tmp_path):
    t = _tracker(tmp_path, capitol_trades=3.0)
    t.record_fetch("capitol_trades", 0)
    t.state["capitol_trades"].last_fetch_at = datetime.now() - timedelta(days=4)
    degraded = t.degraded_providers()
    assert len(degraded) == 1
    assert "never returned data" in degraded[0][1]


def test_provider_without_threshold_not_health_checked(tmp_path):
    t = ProviderHealthTracker(cache_dir=tmp_path, max_empty_days={})
    t.record_fetch("ad_hoc_provider", 0)
    # Backdate to make it look ancient
    t.state["ad_hoc_provider"].last_fetch_at = datetime.now() - timedelta(days=365)
    assert t.degraded_providers() == []


def test_unfetched_provider_not_degraded(tmp_path):
    # Provider has a threshold configured but has never been observed
    t = _tracker(tmp_path, capitol_trades=3.0)
    assert t.degraded_providers() == []


def test_berkshire_quarterly_threshold_tolerant_of_long_silence(tmp_path):
    # Berkshire's filings are quarterly so an 80-day silence is normal;
    # a 100-day threshold should not flag it.
    t = _tracker(tmp_path, berkshire_13f=100.0)
    t.record_fetch("berkshire_13f", 12)
    t.state["berkshire_13f"].last_non_empty_at = datetime.now() - timedelta(days=80)
    assert t.degraded_providers() == []


# ---------------------------------------------------------------------------
# Persistence — JSON round-trip
# ---------------------------------------------------------------------------

def test_state_round_trips_through_disk(tmp_path):
    t1 = _tracker(tmp_path)
    t1.record_fetch("capitol_trades", 4)
    t1.record_fetch("sec_edgar", 0)
    t1.record_fetch("sec_edgar", 0)

    t2 = _tracker(tmp_path)
    assert t2.state["capitol_trades"].last_non_empty_count == 4
    assert t2.state["sec_edgar"].consecutive_empty_fetches == 2
    assert t2.state["sec_edgar"].last_non_empty_at is None


def test_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "provider_health.json").write_text("not valid json {")
    t = _tracker(tmp_path)
    assert t.state == {}
    # Should still be usable
    t.record_fetch("capitol_trades", 1)
    assert t.state["capitol_trades"].last_non_empty_count == 1


def test_missing_state_file_starts_fresh(tmp_path):
    t = _tracker(tmp_path)
    assert t.state == {}


# ---------------------------------------------------------------------------
# Regression scenario: replays the 2026-04 outage
# ---------------------------------------------------------------------------

def test_replays_2026_april_capitol_trades_outage(tmp_path):
    """End-to-end simulation of the bug pattern that motivated this guard.

    Capitol Trades returned 0 filings on every fetch for ~3 days (the API
    endpoint had been retired). Before this guard, that was indistinguishable
    from a quiet day. After this guard, the warning fires once 3 days have
    passed since the last non-empty fetch.
    """
    t = _tracker(tmp_path, capitol_trades=3.0)
    # Day 0: provider working, last good fetch
    t.record_fetch("capitol_trades", 24)
    t.state["capitol_trades"].last_non_empty_at = datetime.now() - timedelta(days=4)
    # Days 1-4: API broken, every cache miss returns 0
    for _ in range(4):
        t.record_fetch("capitol_trades", 0)

    degraded = t.degraded_providers()
    assert [n for n, _ in degraded] == ["capitol_trades"]
    reason = degraded[0][1]
    assert "5 consecutive empty fetches" in reason or "consecutive empty fetches" in reason
    assert "24 filings" in reason
