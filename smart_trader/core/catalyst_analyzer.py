"""CatalystAnalyzer — news/event validation for technical entry signals.

Fetches recent news for any set of symbols (via yfinance, optional Finnhub),
classifies headlines by catalyst type and sentiment, and caches results to
avoid redundant API calls within a single trading day.

This is a signal ENRICHMENT layer, not a signal GATE. Callers receive a
Dict[symbol, List[CatalystEvent]] and apply confidence adjustments as they
see fit. The analyzer itself has no opinion on whether to trade.

Supported news sources (no API key required by default):
  - yfinance Ticker.news  (primary — zero-auth, already a dependency)
  - Finnhub company-news  (optional — set FINNHUB_API_KEY in .env for better
                           coverage and explicit sentiment data)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from smart_trader.qwen.catalyst_classifier import CatalystClassifier

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Catalyst classification
# ---------------------------------------------------------------------------

# Each entry: (regex_pattern, catalyst_type, sentiment)
# First match wins. Patterns are case-insensitive.
_CATALYST_RULES: List[Tuple[str, str, float]] = [
    # Strong positive
    (r"\b(beat|beats|topped?|surpassed?|exceed|exceeds?|blew? past)\b.{0,60}\b(estimate|forecast|expectation|consensus|eps|revenue)", "earnings_beat", 1.0),
    (r"\b(eps|earnings?|profit)\b.{0,40}\b(beat|above|top|surge|jump)", "earnings_beat", 1.0),
    (r"\b(raises?|raised?|boosted?|lifting?|lifted?|increased?)\b.{0,30}\b(guidance|outlook|forecast)\b", "guidance_raise", 1.0),
    (r"\b(acquires?|acquired?|merger|buyout|takeover)\b", "acquisition", 1.0),
    # Moderate positive — analyst upgrade must come before guidance_raise to avoid
    # "raises price target" matching guidance_raise first.
    (r"\b(upgraded?|upgrades?)\b.{0,50}\b(buy|outperform|overweight|strong buy|positive)", "analyst_upgrade", 0.7),
    (r"\b(price target|pt)\b.{0,30}\b(raised?|increased?|boosted?|lifted?|hiked?)", "analyst_upgrade", 0.7),
    (r"\b(raises?|raised?)\b.{0,15}\b(price target|pt)\b", "analyst_upgrade", 0.7),
    # Product launch: "launches" / "unveils" alone is distinctive enough; drop the mandatory noun.
    (r"\b(launches?|unveiled?|unveils?|introduces?)\b", "product_launch", 0.6),
    (r"\b(announced?|released?)\b.{0,40}\b(product|service|platform|feature|drug|device|model|app|solution)\b", "product_launch", 0.6),
    (r"\b(partnership|partnered?)\b.{0,40}\b(sign|signed?|won?|awarded?|selected?|with\b)", "partnership", 0.6),
    (r"\bwins?\b.{0,30}\b(contract|deal|agreement)\b", "partnership", 0.6),
    (r"\b(buyback|repurchase)\b.{0,30}\b(program|plan|authoriz)", "buyback", 0.6),
    # Negative
    (r"\b(miss|misses|missed|fell? short|below|disappoints?|disappointing)\b.{0,40}\b(estimate|forecast|expectation|eps|revenue)", "earnings_miss", -1.0),
    (r"\b(eps|earnings?|profit)\b.{0,40}\b(miss|below|fell? short|disappoint|declin)", "earnings_miss", -1.0),
    (r"\b(cut|cuts|lowers?|lowered?|reduced?)\b.{0,30}\b(guidance|outlook|forecast)\b", "guidance_cut", -1.0),
    (r"\b(downgraded?|downgrades?)\b.{0,50}\b(sell|underperform|underweight|hold|neutral)", "downgrade", -0.8),
    (r"\b(price target|pt)\b.{0,30}\b(cut|lowered?|reduced?|decrease)", "downgrade", -0.6),
    (r"\b(lawsuit|sued?|litigation|class[ -]action|investigation|subpoena)\b", "lawsuit", -0.8),
    (r"\b(recall|recalled?|safety warning|halt|banned?|probe)\b", "recall", -0.8),
]

_RULE_PATTERN_CACHE: Optional[List[Tuple[re.Pattern, str, float]]] = None


def _compiled_rules() -> List[Tuple[re.Pattern, str, float]]:
    global _RULE_PATTERN_CACHE
    if _RULE_PATTERN_CACHE is None:
        _RULE_PATTERN_CACHE = [
            (re.compile(pat, re.IGNORECASE), ctype, sent)
            for pat, ctype, sent in _CATALYST_RULES
        ]
    return _RULE_PATTERN_CACHE


def _classify_headline(headline: str) -> Tuple[str, float]:
    """Return (catalyst_type, sentiment) for a headline. Falls back to ("other", 0.0)."""
    for pattern, ctype, sentiment in _compiled_rules():
        if pattern.search(headline):
            return ctype, sentiment
    return "other", 0.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CatalystEvent:
    symbol: str
    headline: str
    catalyst_type: str   # see _CATALYST_RULES for possible values + "other"
    sentiment: float     # +1.0 positive, -1.0 negative, 0.0 neutral
    published_at: datetime
    source: str          # "yfinance" | "finnhub"
    url: str
    confidence: float = 1.0  # [0.0, 1.0], defaults to 1.0 for regex-classified


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class CatalystAnalyzer:
    """Fetches and classifies recent news for a batch of stock symbols.

    Results are cached to disk (one JSON file per symbol per calendar date)
    with a configurable TTL. On any fetch failure the symbol is returned with
    an empty event list — downstream callers treat that as "no catalyst found".
    """

    def __init__(
        self,
        config,
        cache_dir: str = "smart_trader/cache/catalyst",
        classifier: Optional["CatalystClassifier"] = None,
    ):
        self._cfg = config
        self._cache_dir = Path(cache_dir)
        # Disk caching is an optimization, not a requirement — analyze() works
        # fine without it (see _load_cache/_save_cache). A read-only or
        # unwritable filesystem must NOT disable the whole catalyst feature, so
        # a failed mkdir degrades to "no cache" instead of raising.
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_enabled = True
        except OSError as e:
            logger.warning(
                f"catalyst: cache dir {self._cache_dir} not writable ({e}); "
                f"continuing without disk cache"
            )
            self._cache_enabled = False
        self._finnhub_key: str = os.environ.get("FINNHUB_API_KEY", "")
        self._classifier = classifier

    # ---------------------------------------------------------------- public

    def analyze(self, symbols: List[str]) -> Dict[str, List[CatalystEvent]]:
        """Return catalyst events for each symbol (last lookback_hours of news).

        Empty list for a symbol means no qualifying news was found.
        """
        out: Dict[str, List[CatalystEvent]] = {}
        lookback = timedelta(hours=self._cfg.catalyst_news_lookback_hours)
        cutoff = datetime.now(tz=timezone.utc) - lookback

        for sym in symbols:
            try:
                out[sym] = self._analyze_one(sym.upper(), cutoff)
            except Exception as e:
                logger.warning(f"  catalyst: failed to analyze {sym}: {e}")
                out[sym] = []
        return out

    # --------------------------------------------------------------- private

    def _analyze_one(self, symbol: str, cutoff: datetime) -> List[CatalystEvent]:
        cached = self._load_cache(symbol)
        if cached is not None:
            return [e for e in cached if e.published_at >= cutoff]

        events: List[CatalystEvent] = []

        # Primary: yfinance (zero-auth)
        events.extend(self._fetch_yfinance(symbol))

        # Optional: Finnhub (requires API key in env)
        if self._finnhub_key:
            events.extend(self._fetch_finnhub(symbol))

        # Deduplicate by headline (yfinance and Finnhub sometimes cover same story)
        seen: set = set()
        deduped: List[CatalystEvent] = []
        for e in events:
            key = e.headline[:80].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(e)

        # Optional: re-classify via CatalystClassifier (Qwen-enhanced) if available
        if self._classifier is not None:
            headline_dicts = [
                {
                    "headline": e.headline,
                    "symbol": e.symbol,
                    "published_at": e.published_at,
                    "source": e.source,
                    "url": e.url,
                }
                for e in deduped
            ]
            deduped = self._classifier.classify_batch(headline_dicts)

        self._save_cache(symbol, deduped)
        return [e for e in deduped if e.published_at >= cutoff]

    # ---------------------------------------------------- yfinance fetching

    def _fetch_yfinance(self, symbol: str) -> List[CatalystEvent]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            raw_news = ticker.news or []
        except Exception as e:
            logger.debug(f"  catalyst yfinance: fetch failed for {symbol}: {e}")
            return []

        events: List[CatalystEvent] = []
        for item in raw_news:
            try:
                content = item.get("content", {})
                headline = content.get("title", "").strip()
                if not headline:
                    continue

                pub_str = content.get("pubDate") or content.get("displayTime", "")
                try:
                    pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                url = (
                    (content.get("canonicalUrl") or {}).get("url", "")
                    or (content.get("clickThroughUrl") or {}).get("url", "")
                )
                provider = (content.get("provider") or {}).get("displayName", "yfinance")

                ctype, sentiment = _classify_headline(headline)
                events.append(CatalystEvent(
                    symbol=symbol,
                    headline=headline,
                    catalyst_type=ctype,
                    sentiment=sentiment,
                    published_at=pub_dt,
                    source=f"yfinance/{provider}",
                    url=url,
                ))
            except Exception as e:
                logger.debug(f"  catalyst yfinance: parse error for {symbol} item: {e}")
                continue

        logger.debug(f"  catalyst yfinance: {len(events)} articles for {symbol}")
        return events

    # ---------------------------------------------------- Finnhub fetching

    def _fetch_finnhub(self, symbol: str) -> List[CatalystEvent]:
        try:
            import requests
            today = datetime.now(tz=timezone.utc)
            yesterday = today - timedelta(days=3)
            url = (
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={symbol}"
                f"&from={yesterday.strftime('%Y-%m-%d')}"
                f"&to={today.strftime('%Y-%m-%d')}"
                f"&token={self._finnhub_key}"
            )
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            logger.debug(f"  catalyst finnhub: fetch failed for {symbol}: {e}")
            return []

        events: List[CatalystEvent] = []
        for item in (raw or []):
            try:
                headline = (item.get("headline") or "").strip()
                if not headline:
                    continue
                ts = item.get("datetime", 0)
                pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                ctype, sentiment = _classify_headline(headline)
                events.append(CatalystEvent(
                    symbol=symbol,
                    headline=headline,
                    catalyst_type=ctype,
                    sentiment=sentiment,
                    published_at=pub_dt,
                    source="finnhub",
                    url=item.get("url", ""),
                ))
            except Exception as e:
                logger.debug(f"  catalyst finnhub: parse error for {symbol}: {e}")
                continue

        logger.debug(f"  catalyst finnhub: {len(events)} articles for {symbol}")
        return events

    # ---------------------------------------------------- disk cache helpers

    def _cache_path(self, symbol: str) -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return self._cache_dir / f"{symbol}_{date_str}.json"

    def _load_cache(self, symbol: str) -> Optional[List[CatalystEvent]]:
        if not self._cache_enabled:
            return None
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        age_hours = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
        if age_hours > self._cfg.catalyst_cache_ttl_hours:
            return None
        try:
            raw = json.loads(path.read_text())
            events: List[CatalystEvent] = []
            for d in raw:
                d["published_at"] = datetime.fromisoformat(d["published_at"])
                # Handle old cache entries missing the confidence field
                d.setdefault("confidence", 1.0)
                events.append(CatalystEvent(**d))
            return events
        except Exception as e:
            logger.debug(f"  catalyst cache: load failed for {symbol}: {e}")
            return None

    def _save_cache(self, symbol: str, events: List[CatalystEvent]) -> None:
        if not self._cache_enabled:
            return
        path = self._cache_path(symbol)
        try:
            serializable = []
            for e in events:
                d = asdict(e)
                d["published_at"] = e.published_at.isoformat()
                serializable.append(d)
            path.write_text(json.dumps(serializable, indent=2))
        except Exception as e:
            logger.debug(f"  catalyst cache: save failed for {symbol}: {e}")
