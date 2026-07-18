"""Tests for CatalystAnalyzer and _catalyst_confidence_delta."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from smart_trader.core.catalyst_analyzer import (
    CatalystAnalyzer,
    CatalystEvent,
    _classify_headline,
)
from smart_trader.core.trading_filter import _catalyst_confidence_delta
from smart_trader.settings.config import SmartMoneyConfig


# ---------------------------------------------------------------------------
# _classify_headline tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headline,expected_type,expected_sign", [
    ("Apple beats earnings estimates by 15%", "earnings_beat", 1),
    ("NVIDIA exceeds EPS forecast", "earnings_beat", 1),
    ("Microsoft raises guidance for fiscal year", "guidance_raise", 1),
    ("Berkshire acquires Pilot Flying J", "acquisition", 1),
    ("Goldman Sachs upgraded to Buy", "analyst_upgrade", 1),
    ("Morgan Stanley raises price target for Tesla", "analyst_upgrade", 1),
    ("Apple launches iPhone 17 Pro", "product_launch", 1),
    ("Apple announces buyback program", "buyback", 1),
    ("Tesla misses earnings estimates", "earnings_miss", -1),
    ("Amazon revenue fell short of expectations", "earnings_miss", -1),
    ("Intel cuts guidance for Q4", "guidance_cut", -1),
    ("Pfizer downgraded to Sell by JPMorgan", "downgrade", -1),
    ("Company faces class-action lawsuit", "lawsuit", -1),
    ("Drug recall issued by FDA", "recall", -1),
    ("Stock trades sideways on light volume", "other", 0),
])
def test_classify_headline(headline, expected_type, expected_sign):
    ctype, sentiment = _classify_headline(headline)
    assert ctype == expected_type, f"'{headline}' → got {ctype!r}, want {expected_type!r}"
    if expected_sign > 0:
        assert sentiment > 0, f"expected positive sentiment, got {sentiment}"
    elif expected_sign < 0:
        assert sentiment < 0, f"expected negative sentiment, got {sentiment}"
    else:
        assert sentiment == 0.0, f"expected neutral, got {sentiment}"


# ---------------------------------------------------------------------------
# _catalyst_confidence_delta tests (trading_filter helper)
# ---------------------------------------------------------------------------

def _event(catalyst_type: str, sentiment: float) -> CatalystEvent:
    return CatalystEvent(
        symbol="AAPL",
        headline="test",
        catalyst_type=catalyst_type,
        sentiment=sentiment,
        published_at=datetime.now(tz=timezone.utc),
        source="test",
        url="",
    )


def test_delta_no_events_gap_up_applies_penalty():
    cfg = SmartMoneyConfig()
    delta, top = _catalyst_confidence_delta([], cfg, is_gap_up=True)
    assert delta == pytest.approx(-cfg.catalyst_gap_penalty_no_news)
    assert top is None


def test_delta_no_events_pullback_no_penalty():
    cfg = SmartMoneyConfig()
    delta, top = _catalyst_confidence_delta([], cfg, is_gap_up=False)
    assert delta == 0.0
    assert top is None


def test_delta_strong_positive_gap_up():
    cfg = SmartMoneyConfig()
    events = [_event("earnings_beat", 1.0)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=True)
    assert delta == pytest.approx(cfg.catalyst_gap_boost_strong)
    assert top is not None
    assert top.catalyst_type == "earnings_beat"


def test_delta_strong_positive_pullback():
    cfg = SmartMoneyConfig()
    events = [_event("acquisition", 1.0)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=False)
    assert delta == pytest.approx(cfg.catalyst_pullback_boost_strong)


def test_delta_moderate_positive_gap_up():
    cfg = SmartMoneyConfig()
    events = [_event("analyst_upgrade", 0.7)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=True)
    assert delta == pytest.approx(cfg.catalyst_gap_boost_moderate)


def test_delta_moderate_positive_pullback():
    cfg = SmartMoneyConfig()
    events = [_event("product_launch", 0.6)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=False)
    assert delta == pytest.approx(cfg.catalyst_pullback_boost_moderate)


def test_delta_negative_gap_up():
    cfg = SmartMoneyConfig()
    events = [_event("earnings_miss", -1.0)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=True)
    assert delta == pytest.approx(-cfg.catalyst_gap_penalty_negative)


def test_delta_negative_pullback():
    cfg = SmartMoneyConfig()
    events = [_event("downgrade", -0.8)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=False)
    assert delta == pytest.approx(-cfg.catalyst_pullback_penalty_negative)


def test_delta_picks_highest_magnitude_event():
    """When multiple events exist, the highest-magnitude one drives the delta."""
    cfg = SmartMoneyConfig()
    events = [
        _event("analyst_upgrade", 0.7),   # moderate
        _event("earnings_beat", 1.0),      # strong — should win
    ]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=True)
    assert delta == pytest.approx(cfg.catalyst_gap_boost_strong)
    assert top.catalyst_type == "earnings_beat"


def test_delta_other_neutral_no_adjustment():
    cfg = SmartMoneyConfig()
    events = [_event("other", 0.0)]
    delta, top = _catalyst_confidence_delta(events, cfg, is_gap_up=True)
    assert delta == 0.0


# ---------------------------------------------------------------------------
# CatalystAnalyzer.analyze() — yfinance fetch + cache
# ---------------------------------------------------------------------------

def _make_yf_news_item(title: str, pub_iso: str, url: str = "") -> dict:
    """Construct a yfinance-style news dict (2025+ nested structure)."""
    return {
        "id": "test-id",
        "content": {
            "title": title,
            "pubDate": pub_iso,
            "displayTime": pub_iso,
            "provider": {"displayName": "Test Publisher"},
            "canonicalUrl": {"url": url},
            "clickThroughUrl": {"url": url},
        },
    }


def _make_analyzer(cfg: SmartMoneyConfig = None) -> CatalystAnalyzer:
    if cfg is None:
        cfg = SmartMoneyConfig()
    with tempfile.TemporaryDirectory() as tmp:
        analyzer = CatalystAnalyzer(cfg, cache_dir=tmp)
        analyzer._cache_dir_tmp = tmp  # keep reference so dir isn't cleaned up
        return analyzer


@pytest.fixture()
def tmp_analyzer():
    cfg = SmartMoneyConfig()
    with tempfile.TemporaryDirectory() as tmp:
        yield CatalystAnalyzer(cfg, cache_dir=tmp), tmp


def test_analyze_returns_events_from_yfinance(tmp_analyzer):
    analyzer, _ = tmp_analyzer
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    mock_news = [
        _make_yf_news_item("Apple beats EPS estimates", now_iso),
        _make_yf_news_item("Apple launches new product line", now_iso),
    ]
    mock_ticker = MagicMock()
    mock_ticker.news = mock_news

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = analyzer.analyze(["AAPL"])

    assert "AAPL" in result
    events = result["AAPL"]
    assert len(events) == 2
    types = {e.catalyst_type for e in events}
    assert "earnings_beat" in types
    assert "product_launch" in types


def test_analyze_filters_old_news(tmp_analyzer):
    analyzer, _ = tmp_analyzer
    old_iso = (datetime.now(tz=timezone.utc) - timedelta(hours=72)).isoformat()
    mock_news = [_make_yf_news_item("Stale headline from 3 days ago", old_iso)]
    mock_ticker = MagicMock()
    mock_ticker.news = mock_news

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = analyzer.analyze(["AAPL"])

    assert result["AAPL"] == []  # older than 48h lookback


def test_analyze_returns_empty_on_fetch_failure(tmp_analyzer):
    analyzer, _ = tmp_analyzer

    with patch("yfinance.Ticker", side_effect=RuntimeError("network error")):
        result = analyzer.analyze(["AAPL"])

    assert result["AAPL"] == []


def test_analyze_uses_cache_on_second_call(tmp_analyzer):
    analyzer, _ = tmp_analyzer
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    mock_news = [_make_yf_news_item("Apple beats earnings", now_iso)]
    mock_ticker = MagicMock()
    mock_ticker.news = mock_news

    with patch("yfinance.Ticker", return_value=mock_ticker) as mock_cls:
        analyzer.analyze(["AAPL"])   # first call — fetches
        analyzer.analyze(["AAPL"])   # second call — should use cache

    # yfinance.Ticker should only be instantiated once (cache hit on second call)
    assert mock_cls.call_count == 1


def test_analyze_multiple_symbols(tmp_analyzer):
    analyzer, _ = tmp_analyzer
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    def _ticker_factory(sym):
        t = MagicMock()
        if sym == "TSLA":
            t.news = [_make_yf_news_item("Tesla raises guidance", now_iso)]
        else:
            t.news = []
        return t

    with patch("yfinance.Ticker", side_effect=_ticker_factory):
        result = analyzer.analyze(["AAPL", "TSLA"])

    assert result["AAPL"] == []
    assert len(result["TSLA"]) == 1
    assert result["TSLA"][0].catalyst_type == "guidance_raise"
