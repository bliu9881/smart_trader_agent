"""
CatalystClassifier — Qwen-enhanced headline classification.

Enhances the existing regex-based _classify_headline with Qwen-powered
classification. Always runs regex first as baseline, then optionally
calls Qwen for enhanced results. On any Qwen failure, falls back to
regex results with confidence=1.0.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from smart_trader.core.catalyst_analyzer import CatalystEvent, _classify_headline
from smart_trader.qwen.client import QwenClient, QwenError, QwenResponse
from smart_trader.settings.config import QwenAgentConfig

logger = logging.getLogger(__name__)

# Maximum headlines per Qwen request batch
_MAX_BATCH_SIZE = 20

# Valid catalyst types for validation
_VALID_CATALYST_TYPES = frozenset([
    "earnings_beat", "guidance_raise", "acquisition", "analyst_upgrade",
    "product_launch", "partnership", "buyback", "earnings_miss",
    "guidance_cut", "downgrade", "lawsuit", "recall", "other",
])

_SYSTEM_PROMPT = (
    "You are a financial news classifier. Classify each headline into exactly one "
    "category and provide a sentiment score.\n"
    "Categories: earnings_beat, guidance_raise, acquisition, analyst_upgrade, "
    "product_launch, partnership, buyback, earnings_miss, guidance_cut, downgrade, "
    "lawsuit, recall, other\n"
    "Respond with a JSON array, one object per headline: "
    '[{"catalyst_type": "...", "sentiment": float_between_-1_and_1, '
    '"confidence": float_between_0_and_1}]'
)


class CatalystClassifier:
    """Enhances headline classification with Qwen AI.

    Flow:
    1. Run regex _classify_headline on each headline (always)
    2. If config.catalyst_classification_enabled, batch headlines to Qwen
    3. Merge Qwen results: override regex classification, add confidence
    4. On any Qwen failure: return regex results with confidence=1.0
    """

    def __init__(self, client: QwenClient, config: QwenAgentConfig) -> None:
        self._client = client
        self._config = config

    def classify_batch(self, headlines: List[dict]) -> List[CatalystEvent]:
        """Classify a list of headline dicts into CatalystEvents.

        Each headline dict must contain at minimum:
            {"headline": str, "symbol": str, "published_at": datetime|str, "source": str, "url": str}

        Returns exactly one CatalystEvent per input headline.
        """
        if not headlines:
            return []

        # Step 1: Always run regex classification first
        regex_events = self._regex_classify_all(headlines)

        # Step 2: If Qwen classification is disabled, return regex results
        if not self._config.catalyst_classification_enabled:
            return regex_events

        # Step 3: Batch headlines to Qwen in groups of up to 20
        qwen_results = self._qwen_classify_batched(headlines)

        # Step 4: Merge results — Qwen overrides regex when available
        if qwen_results is None:
            return regex_events

        return self._merge_results(regex_events, qwen_results)

    def _regex_classify_all(self, headlines: List[dict]) -> List[CatalystEvent]:
        """Run regex classification on all headlines."""
        events: List[CatalystEvent] = []
        for h in headlines:
            headline_text = h.get("headline", "")
            symbol = h.get("symbol", "")
            catalyst_type, sentiment = _classify_headline(headline_text)

            published_at = h.get("published_at")
            if isinstance(published_at, str):
                try:
                    published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    published_at = datetime.now(tz=timezone.utc)
            elif published_at is None:
                published_at = datetime.now(tz=timezone.utc)

            events.append(CatalystEvent(
                symbol=symbol,
                headline=headline_text,
                catalyst_type=catalyst_type,
                sentiment=sentiment,
                published_at=published_at,
                source=h.get("source", "unknown"),
                url=h.get("url", ""),
                confidence=1.0,
            ))
        return events

    def _qwen_classify_batched(
        self, headlines: List[dict]
    ) -> Optional[List[dict]]:
        """Send headlines to Qwen in batches of up to 20. Returns parsed results or None on failure."""
        all_results: List[dict] = []

        for batch_start in range(0, len(headlines), _MAX_BATCH_SIZE):
            batch = headlines[batch_start:batch_start + _MAX_BATCH_SIZE]
            batch_result = self._qwen_classify_single_batch(batch)
            if batch_result is None:
                # On any batch failure, abort and fall back to regex for ALL headlines
                return None
            all_results.extend(batch_result)

        return all_results

    def _qwen_classify_single_batch(
        self, batch: List[dict]
    ) -> Optional[List[dict]]:
        """Classify a single batch (≤20 headlines) via Qwen. Returns parsed list or None."""
        # Determine symbol from the batch (use first headline's symbol for prompt context)
        symbol = batch[0].get("symbol", "UNKNOWN") if batch else "UNKNOWN"

        # Build user message
        headline_lines = []
        for i, h in enumerate(batch, start=1):
            headline_lines.append(f'{i}. "{h.get("headline", "")}"')

        user_content = (
            f"Classify these headlines for {symbol}:\n"
            + "\n".join(headline_lines)
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = self._client.chat(
            messages=messages,
            temperature=0.1,
            max_tokens=200 + len(batch) * 50,
            response_format={"type": "json_object"},
        )

        # Handle Qwen error
        if isinstance(response, QwenError):
            logger.warning(
                "Catalyst classification Qwen call failed: category=%s desc=%s — using regex fallback",
                response.error_category,
                response.description,
            )
            return None

        # Parse Qwen response
        return self._parse_qwen_response(response, expected_count=len(batch))

    def _parse_qwen_response(
        self, response: QwenResponse, expected_count: int
    ) -> Optional[List[dict]]:
        """Parse the Qwen JSON response into a list of classification dicts.

        Returns None if response is malformed.
        """
        try:
            content = response.content.strip()
            parsed = json.loads(content)

            # Handle both {"results": [...]} and direct [...] formats
            if isinstance(parsed, dict):
                # Try common wrapper keys
                for key in ("results", "classifications", "headlines"):
                    if key in parsed and isinstance(parsed[key], list):
                        parsed = parsed[key]
                        break
                else:
                    # If it's a dict but none of our known keys, it's malformed
                    logger.warning(
                        "Catalyst classification: malformed Qwen response (unexpected dict structure)"
                    )
                    return None

            if not isinstance(parsed, list):
                logger.warning(
                    "Catalyst classification: malformed Qwen response (not a list)"
                )
                return None

            if len(parsed) != expected_count:
                logger.warning(
                    "Catalyst classification: Qwen returned %d items, expected %d — discarding",
                    len(parsed), expected_count,
                )
                return None

            # Validate each entry
            results: List[dict] = []
            for item in parsed:
                if not isinstance(item, dict):
                    logger.warning(
                        "Catalyst classification: malformed item in Qwen response (not a dict)"
                    )
                    return None

                catalyst_type = item.get("catalyst_type", "")
                sentiment = item.get("sentiment")
                confidence = item.get("confidence")

                # Validate catalyst_type
                if catalyst_type not in _VALID_CATALYST_TYPES:
                    catalyst_type = "other"

                # Validate sentiment
                try:
                    sentiment = float(sentiment)
                    sentiment = max(-1.0, min(1.0, sentiment))
                except (TypeError, ValueError):
                    logger.warning(
                        "Catalyst classification: invalid sentiment value in Qwen response"
                    )
                    return None

                # Validate confidence
                try:
                    confidence = float(confidence)
                    confidence = max(0.0, min(1.0, confidence))
                except (TypeError, ValueError):
                    logger.warning(
                        "Catalyst classification: invalid confidence value in Qwen response"
                    )
                    return None

                results.append({
                    "catalyst_type": catalyst_type,
                    "sentiment": sentiment,
                    "confidence": confidence,
                })

            return results

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Catalyst classification: failed to parse Qwen response: %s: %s",
                type(exc).__name__, str(exc)[:200],
            )
            return None

    def _merge_results(
        self, regex_events: List[CatalystEvent], qwen_results: List[dict]
    ) -> List[CatalystEvent]:
        """Merge Qwen results into regex events, overriding classification."""
        merged: List[CatalystEvent] = []
        for event, qwen in zip(regex_events, qwen_results):
            merged.append(CatalystEvent(
                symbol=event.symbol,
                headline=event.headline,
                catalyst_type=qwen["catalyst_type"],
                sentiment=qwen["sentiment"],
                published_at=event.published_at,
                source=event.source,
                url=event.url,
                confidence=qwen["confidence"],
            ))
        return merged
