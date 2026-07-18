"""
Signal Arbitrator — Qwen-powered ranking of multiple candidate signals.

When the rule engine produces two or more entry/exit candidates in a single
cycle, the Signal Arbitrator asks Qwen to rank them by priority given the
current portfolio context (positions, exposure, sector allocation, cash,
market regime). The result is an ActionPlan with prioritized signals and
per-signal reasoning.

Gated-mode constraints:
  - Cannot add new symbols not in the original candidate list
  - Cannot modify entry_price, stop_loss, take_profit, trailing_stop_pct,
    or position_size_pct
  - On any failure: returns candidates in original order (graceful degradation)
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from smart_trader.core.signal import Signal
from smart_trader.qwen.client import QwenClient, QwenError, QwenResponse
from smart_trader.settings.config import QwenAgentConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ActionPlanEntry:
    """A single ranked signal with reasoning."""
    signal: Signal
    reasoning: str  # max 500 chars
    rank: int


@dataclass
class ActionPlan:
    """Prioritized list of signals produced by Qwen ranking."""
    entries: List[ActionPlanEntry] = field(default_factory=list)
    raw_qwen_response: Optional[str] = None


# ---------------------------------------------------------------------------
# Signal Arbitrator
# ---------------------------------------------------------------------------

class SignalArbitrator:
    """Ranks multiple candidate signals using Qwen and portfolio context.

    Key behaviors:
    - If fewer than 2 candidates or arbitration disabled: pass through unchanged
    - Calls Qwen with portfolio context to rank signals
    - Validates that Qwen's output respects gated-mode constraints
    - On any failure: returns original candidates in original order
    """

    def __init__(self, client: QwenClient, config: QwenAgentConfig) -> None:
        self._client = client
        self._config = config

    def rank(
        self,
        candidates: List[Signal],
        portfolio_state: Any,  # PortfolioState or dict
        regime_state: Optional[dict] = None,
    ) -> List[Signal]:
        """Rank candidate signals using Qwen.

        Args:
            candidates: List of signals from the rule engine.
            portfolio_state: Current portfolio state (PortfolioState dataclass or dict).
            regime_state: Optional market regime info (zone, posture).

        Returns:
            Signals in priority order (Qwen-ranked or original on failure/bypass).
        """
        # Bypass: too few candidates or feature disabled
        if len(candidates) < 2:
            return candidates
        if not self._config.signal_arbitration_enabled:
            return candidates

        try:
            # Build the prompt
            messages = self._build_messages(candidates, portfolio_state, regime_state)

            # Call Qwen
            result: Union[QwenResponse, QwenError] = self._client.chat(
                messages=messages,
                temperature=0.3,
                max_tokens=800,
                response_format={"type": "json_object"},
            )

            # Handle Qwen error
            if isinstance(result, QwenError):
                logger.warning(
                    "Signal arbitration skipped: Qwen returned error | "
                    "category=%s description=%s",
                    result.error_category,
                    result.description,
                )
                return candidates

            # Parse Qwen response into ActionPlan
            action_plan = self._parse_response(result.content, candidates)
            if action_plan is None:
                logger.warning(
                    "Signal arbitration skipped: failed to parse Qwen response"
                )
                return candidates

            # Validate and return
            validated = self._validate_action_plan(action_plan, candidates)
            return validated

        except Exception as exc:
            logger.warning(
                "Signal arbitration skipped: unexpected error | %s: %s",
                type(exc).__name__,
                str(exc)[:200],
            )
            return candidates

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        candidates: List[Signal],
        portfolio_state: Any,
        regime_state: Optional[dict],
    ) -> List[dict]:
        """Build chat messages for signal ranking."""
        # Extract portfolio info (handle both PortfolioState dataclass and dict)
        portfolio_info = self._extract_portfolio_info(portfolio_state)

        # Build candidate descriptions
        candidate_descriptions = []
        for i, sig in enumerate(candidates):
            desc = {
                "index": i,
                "symbol": sig.symbol,
                "direction": sig.direction,
                "confidence": round(sig.confidence, 4),
                "entry_price": round(sig.entry_price, 2),
                "reasoning": sig.reasoning[:200] if sig.reasoning else "",
            }
            candidate_descriptions.append(desc)

        # System prompt
        system_content = (
            "You are a trading signal arbitrator. Given a list of candidate trading "
            "signals and portfolio context, rank them by priority. Consider portfolio "
            "diversification, sector exposure, correlation risk, and signal quality. "
            "Return a JSON object with a 'rankings' array. Each element must have: "
            "'symbol' (string), 'rank' (integer starting at 1), and 'reasoning' "
            "(string, max 500 characters explaining why this rank). "
            "You may only rank symbols from the provided candidate list. "
            "You may exclude candidates you think should be filtered out."
        )

        # User prompt
        user_data = {
            "candidates": candidate_descriptions,
            "portfolio": portfolio_info,
        }
        if regime_state:
            user_data["regime"] = regime_state

        user_content = (
            "Rank these trading signal candidates by priority given the portfolio "
            "context below. Return JSON with a 'rankings' array.\n\n"
            f"{json.dumps(user_data, indent=2)}"
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    def _extract_portfolio_info(self, portfolio_state: Any) -> dict:
        """Extract portfolio info from PortfolioState or dict."""
        if portfolio_state is None:
            return {}

        # Handle dict input
        if isinstance(portfolio_state, dict):
            return {
                "equity": portfolio_state.get("equity", 0),
                "cash": portfolio_state.get("cash", 0),
                "positions": list(portfolio_state.get("positions", {}).keys()),
                "total_exposure_pct": portfolio_state.get("total_exposure_pct", 0),
                "sector_exposure": portfolio_state.get("sector_exposure_pct", {}),
            }

        # Handle PortfolioState dataclass
        info: Dict[str, Any] = {
            "equity": round(portfolio_state.equity, 2),
            "cash": round(portfolio_state.cash, 2),
            "positions": list(portfolio_state.positions.keys()),
            "total_exposure_pct": round(portfolio_state.total_exposure_pct, 4),
            "sector_exposure": {
                k: round(v, 4)
                for k, v in portfolio_state.sector_exposure_pct.items()
            },
        }
        return info

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        content: str,
        candidates: List[Signal],
    ) -> Optional[ActionPlan]:
        """Parse Qwen's JSON response into an ActionPlan.

        Expected format:
        {
            "rankings": [
                {"symbol": "AAPL", "rank": 1, "reasoning": "..."},
                {"symbol": "TSLA", "rank": 2, "reasoning": "..."}
            ]
        }
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "Signal arbitration parse error: invalid JSON | %s",
                str(exc)[:100],
            )
            return None

        rankings = data.get("rankings")
        if not isinstance(rankings, list) or len(rankings) == 0:
            logger.warning(
                "Signal arbitration parse error: missing or empty 'rankings' array"
            )
            return None

        # Build symbol→signal lookup from candidates
        symbol_to_signal: Dict[str, Signal] = {
            sig.symbol: sig for sig in candidates
        }

        entries: List[ActionPlanEntry] = []
        for item in rankings:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol", "")
            rank = item.get("rank", 0)
            reasoning = item.get("reasoning", "")

            # Skip unknown symbols (gated-mode enforcement)
            if symbol not in symbol_to_signal:
                logger.warning(
                    "Signal arbitration: Qwen returned unknown symbol '%s', "
                    "discarding",
                    symbol,
                )
                continue

            # Truncate reasoning to max_reasoning_chars
            if len(reasoning) > self._config.max_reasoning_chars:
                reasoning = reasoning[: self._config.max_reasoning_chars]

            entry = ActionPlanEntry(
                signal=symbol_to_signal[symbol],
                reasoning=reasoning,
                rank=int(rank) if isinstance(rank, (int, float)) else 0,
            )
            entries.append(entry)

        if not entries:
            logger.warning(
                "Signal arbitration parse error: no valid entries after parsing"
            )
            return None

        # Sort by rank
        entries.sort(key=lambda e: e.rank)

        return ActionPlan(entries=entries, raw_qwen_response=content)

    # ------------------------------------------------------------------
    # Validation gate
    # ------------------------------------------------------------------

    def _validate_action_plan(
        self,
        action_plan: ActionPlan,
        original_candidates: List[Signal],
    ) -> List[Signal]:
        """Validate the action plan against gated-mode constraints.

        For each entry:
          1. Symbol must exist in original candidates
          2. entry_price, stop_loss, take_profit, trailing_stop_pct,
             position_size_pct must be identical to the original

        Discards invalid entries (logs WARNING per discard).
        If ALL entries discarded: returns original candidates in original order.
        Adds arbitration_reasoning and arbitration_rank to signal.metadata.
        """
        # Build lookup from original candidates
        original_by_symbol: Dict[str, Signal] = {
            sig.symbol: sig for sig in original_candidates
        }

        validated: List[Signal] = []
        for entry in action_plan.entries:
            symbol = entry.signal.symbol

            # Check symbol exists in originals
            if symbol not in original_by_symbol:
                logger.warning(
                    "Validation gate: discarding symbol '%s' — not in original "
                    "candidate list",
                    symbol,
                )
                continue

            original = original_by_symbol[symbol]

            # Check field integrity (the signal object should be the same
            # reference from parsing, but verify key fields match the original)
            if not self._fields_match(entry.signal, original):
                logger.warning(
                    "Validation gate: discarding symbol '%s' — field integrity "
                    "violation",
                    symbol,
                )
                continue

            # Use the original signal (guarantees field integrity) and enrich
            # metadata with arbitration info
            enriched = original
            enriched.metadata["arbitration_reasoning"] = entry.reasoning
            enriched.metadata["arbitration_rank"] = entry.rank
            validated.append(enriched)

        # If all entries invalid, fall back to original order
        if not validated:
            logger.warning(
                "Validation gate: all entries discarded, returning original "
                "candidate order"
            )
            return original_candidates

        return validated

    @staticmethod
    def _fields_match(signal: Signal, original: Signal) -> bool:
        """Check that protected fields are identical between signal and original."""
        if signal.entry_price != original.entry_price:
            return False
        if signal.stop_loss != original.stop_loss:
            return False
        if signal.take_profit != original.take_profit:
            return False
        if signal.trailing_stop_pct != original.trailing_stop_pct:
            return False
        if signal.position_size_pct != original.position_size_pct:
            return False
        return True
