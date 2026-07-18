"""
Commentary Generator — produces human-readable cycle summaries via Qwen.

Runs in a background thread so it never blocks the trading loop.
On failure or empty response, stores a fallback message.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from smart_trader.qwen.client import QwenClient, QwenError
from smart_trader.settings.config import QwenAgentConfig

if TYPE_CHECKING:
    from smart_trader.api.state import StateStore

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a trading portfolio commentator. Summarize what happened this "
    "trading cycle in plain English for the dashboard user."
)

_UNAVAILABLE_MESSAGE = "AI commentary unavailable for this cycle"


class CommentaryGenerator:
    """Generates natural-language commentary for each trading cycle via Qwen."""

    def __init__(
        self,
        client: QwenClient,
        config: QwenAgentConfig,
        state_store: "StateStore",
    ) -> None:
        self._client = client
        self._config = config
        self._state_store = state_store

    def generate_async(self, cycle_context: dict) -> None:
        """Fire-and-forget commentary generation in a background thread.

        Never blocks the caller.
        """
        thread = threading.Thread(
            target=self._generate,
            args=(cycle_context,),
            daemon=True,
        )
        thread.start()

    def _generate(self, cycle_context: dict) -> None:
        """Build prompt, call Qwen, truncate, and store result."""
        cycle_number = cycle_context.get("cycle_number", 0)
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            messages = self._build_messages(cycle_context)
            result = self._client.chat(
                messages=messages,
                temperature=0.5,
                max_tokens=self._config.max_commentary_tokens,
                # Commentary is the heaviest call; give it its own (longer)
                # budget instead of the global 10s api_timeout_seconds.
                timeout=self._config.commentary_timeout_seconds,
            )

            if isinstance(result, QwenError):
                logger.warning(
                    "Commentary generation failed: %s — %s",
                    result.error_category,
                    result.description,
                )
                self._store_unavailable(timestamp, cycle_number)
                return

            content = result.content.strip()
            if not content:
                logger.warning("Commentary generation returned empty response")
                self._store_unavailable(timestamp, cycle_number)
                return

            # Truncate at last complete sentence within max_commentary_chars
            content = _truncate_at_sentence(content, self._config.max_commentary_chars)

            self._state_store.update(
                agent_commentary={
                    "content": content,
                    "timestamp": timestamp,
                    "cycle_number": cycle_number,
                    "status": "available",
                }
            )

        except Exception as exc:
            logger.warning(
                "Commentary generation unexpected error: %s: %s",
                type(exc).__name__,
                str(exc)[:200],
            )
            self._store_unavailable(timestamp, cycle_number)

    def _store_unavailable(self, timestamp: str, cycle_number: int) -> None:
        """Store the unavailable fallback message."""
        self._state_store.update(
            agent_commentary={
                "content": _UNAVAILABLE_MESSAGE,
                "timestamp": timestamp,
                "cycle_number": cycle_number,
                "status": "unavailable",
            }
        )

    def _build_messages(self, cycle_context: dict) -> list:
        """Build the chat messages from cycle context."""
        signals_generated = cycle_context.get("signals_generated", [])
        signals_approved = cycle_context.get("signals_approved", [])
        signals_rejected = cycle_context.get("signals_rejected", [])
        positions = cycle_context.get("positions", {})
        equity = cycle_context.get("equity", 0.0)
        cash = cycle_context.get("cash", 0.0)

        user_content = (
            f"Cycle summary data:\n"
            f"- Signals generated: {len(signals_generated)}\n"
            f"- Signals approved: {len(signals_approved)}\n"
            f"- Signals rejected: {len(signals_rejected)}\n"
            f"- Current positions: {len(positions)}\n"
            f"- Portfolio equity: ${equity:,.2f}\n"
            f"- Cash available: ${cash:,.2f}\n\n"
        )

        if signals_approved:
            user_content += "Approved signals:\n"
            for sig in signals_approved:
                symbol = sig.get("symbol", "?") if isinstance(sig, dict) else str(sig)
                user_content += f"  - {symbol}\n"

        if signals_rejected:
            user_content += "Rejected signals:\n"
            for sig in signals_rejected:
                if isinstance(sig, dict):
                    symbol = sig.get("symbol", "?")
                    reason = sig.get("reason", "unknown")
                    user_content += f"  - {symbol}: {reason}\n"
                else:
                    user_content += f"  - {sig}\n"

        if positions:
            user_content += "Current positions:\n"
            if isinstance(positions, dict):
                for symbol, info in positions.items():
                    user_content += f"  - {symbol}\n"
            elif isinstance(positions, list):
                for pos in positions:
                    symbol = pos.get("symbol", "?") if isinstance(pos, dict) else str(pos)
                    user_content += f"  - {symbol}\n"

        user_content += (
            "\nPlease summarize what happened this cycle: entries made, "
            "exits made, positions held, and candidates skipped/rejected "
            "with primary reasons."
        )

        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last complete sentence within max_chars.

    Looks for the last '.', '!', or '?' within the limit.
    If no sentence boundary found, hard truncates at max_chars.
    """
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]

    # Find the last sentence-ending punctuation
    last_period = truncated.rfind(".")
    last_exclaim = truncated.rfind("!")
    last_question = truncated.rfind("?")

    last_boundary = max(last_period, last_exclaim, last_question)

    if last_boundary > 0:
        return truncated[: last_boundary + 1]

    # No sentence boundary found — hard truncate
    return truncated
