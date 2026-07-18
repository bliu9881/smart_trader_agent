"""
Qwen Cloud HTTP client for Smart Trader.

Provides a synchronous chat completion client for the DashScope-compatible
OpenAI endpoint. All errors are contained — the client never raises to the
caller; it returns a structured QwenError instead.
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import httpx

from smart_trader.settings.config import QwenAgentConfig

logger = logging.getLogger(__name__)


@dataclass
class QwenResponse:
    """Successful response from Qwen API."""
    content: str
    model: str
    usage: dict  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    finish_reason: str


@dataclass
class QwenError:
    """Structured error result — never raises to caller."""
    error_category: str  # "timeout", "network", "server_error", "client_error", "parse_error"
    status_code: Optional[int]
    description: str


class QwenClient:
    """Synchronous HTTP client for Qwen Cloud (DashScope-compatible endpoint).

    Key behaviors:
    - Retries on 5xx/network/timeout up to max_retries with exponential backoff (1s base)
    - Never retries on 4xx
    - Returns QwenError on all failure paths — no exceptions escape
    - Logs latency, model, token usage, success/failure at INFO level
    """

    def __init__(self, config: QwenAgentConfig) -> None:
        """Initialize QwenClient.

        Raises:
            ValueError: If DASHSCOPE_API_KEY environment variable is missing or empty.
        """
        self._config = config
        self._api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not self._api_key:
            raise ValueError(
                "DASHSCOPE_API_KEY environment variable is missing or empty. "
                "Set it to your DashScope API key to use the Qwen agent."
            )
        self._endpoint = f"{config.api_base_url.rstrip('/')}/chat/completions"
        self._timeout = config.api_timeout_seconds
        self._max_retries = config.max_retries
        self._model = config.model_name

    def chat(
        self,
        messages: List[dict],
        temperature: float = 0.3,
        max_tokens: int = 500,
        response_format: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Union[QwenResponse, QwenError]:
        """Send a chat completion request.

        Returns QwenResponse on success, QwenError on any failure. Never raises.
        ``timeout`` overrides the default api_timeout_seconds for this call —
        used by heavier calls (e.g. commentary generation) that need longer.
        """
        request_timeout = timeout if timeout is not None else self._timeout
        body: Dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            body["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[QwenError] = None
        attempts = 1 + self._max_retries  # initial attempt + retries

        for attempt in range(attempts):
            start_time = time.time()
            try:
                response = httpx.post(
                    self._endpoint,
                    json=body,
                    headers=headers,
                    timeout=request_timeout,
                )
                latency = time.time() - start_time
                status = response.status_code

                # 4xx — never retry
                if 400 <= status < 500:
                    error = QwenError(
                        error_category="client_error",
                        status_code=status,
                        description=f"HTTP {status}: {response.text[:200]}",
                    )
                    logger.info(
                        "Qwen request failed | model=%s latency=%.2fs status=%d category=client_error",
                        self._model, latency, status,
                    )
                    return error

                # 5xx — retry
                if status >= 500:
                    last_error = QwenError(
                        error_category="server_error",
                        status_code=status,
                        description=f"HTTP {status}: {response.text[:200]}",
                    )
                    logger.info(
                        "Qwen request failed | model=%s latency=%.2fs status=%d category=server_error attempt=%d/%d",
                        self._model, latency, status, attempt + 1, attempts,
                    )
                    if attempt < attempts - 1:
                        self._backoff(attempt)
                    continue

                # 2xx — parse response
                return self._parse_response(response, latency)

            except httpx.TimeoutException:
                latency = time.time() - start_time
                last_error = QwenError(
                    error_category="timeout",
                    status_code=None,
                    description=f"Request timed out after {request_timeout}s",
                )
                logger.info(
                    "Qwen request failed | model=%s latency=%.2fs category=timeout attempt=%d/%d",
                    self._model, latency, attempt + 1, attempts,
                )
                if attempt < attempts - 1:
                    self._backoff(attempt)

            except httpx.HTTPError as exc:
                latency = time.time() - start_time
                last_error = QwenError(
                    error_category="network",
                    status_code=None,
                    description=f"Network error: {type(exc).__name__}: {str(exc)[:200]}",
                )
                logger.info(
                    "Qwen request failed | model=%s latency=%.2fs category=network attempt=%d/%d",
                    self._model, latency, attempt + 1, attempts,
                )
                if attempt < attempts - 1:
                    self._backoff(attempt)

            except Exception as exc:
                latency = time.time() - start_time
                last_error = QwenError(
                    error_category="network",
                    status_code=None,
                    description=f"Unexpected error: {type(exc).__name__}: {str(exc)[:200]}",
                )
                logger.info(
                    "Qwen request failed | model=%s latency=%.2fs category=network attempt=%d/%d",
                    self._model, latency, attempt + 1, attempts,
                )
                if attempt < attempts - 1:
                    self._backoff(attempt)

        # All retries exhausted
        assert last_error is not None
        logger.warning(
            "Qwen retries exhausted | category=%s endpoint=%s attempts=%d",
            last_error.error_category, self._endpoint, attempts,
        )
        return last_error

    def _parse_response(self, response: httpx.Response, latency: float) -> Union[QwenResponse, QwenError]:
        """Parse a 2xx response into QwenResponse or QwenError on parse failure."""
        try:
            data = response.json()
            choices = data["choices"]
            first_choice = choices[0]
            content = first_choice["message"]["content"]
            finish_reason = first_choice.get("finish_reason", "stop")
            model = data.get("model", self._model)
            usage = data.get("usage", {})

            result = QwenResponse(
                content=content,
                model=model,
                usage=usage,
                finish_reason=finish_reason,
            )

            logger.info(
                "Qwen request succeeded | model=%s latency=%.2fs tokens=%s finish=%s",
                model, latency, usage, finish_reason,
            )
            return result

        except (KeyError, IndexError, TypeError, ValueError) as exc:
            error = QwenError(
                error_category="parse_error",
                status_code=response.status_code,
                description=f"Failed to parse response: {type(exc).__name__}: {str(exc)[:200]}",
            )
            logger.info(
                "Qwen request failed | model=%s latency=%.2fs category=parse_error",
                self._model, latency,
            )
            return error

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff with 1s base delay."""
        delay = 2 ** attempt  # attempt 0 → 1s, attempt 1 → 2s, attempt 2 → 4s, ...
        time.sleep(delay)
