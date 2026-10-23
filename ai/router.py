"""
Client for the AI API at https://9.todict.tech/v1 (OpenAI-compatible router).

Single-model flow: every request goes to ai/models.py -> MODEL (AI_MODEL in
.env). There is no fallback chain any more — if the model fails, the error
is surfaced to the chat instead of silently swapping models mid-answer.

Returns an async generator of StreamChunk so it can be used to stream
"thinking" + answer content to Telegram in real time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from ai.models import MODEL
from config import settings

logger = logging.getLogger("ai.router")

@dataclass
class StreamChunk:
    """A single streamed chunk from the model."""
    kind: str  # "reasoning" | "content"
    delta: str

class ModelRequestError(Exception):
    """Raised when the model call fails (HTTP error, timeout, connection drop)."""

    def __init__(self, model: str, detail: str):
        self.model = model
        super().__init__(f"{model}: {detail}")

class AIRouter:
    def __init__(self) -> None:
        self._base_url = settings.ai_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        }

    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[StreamChunk]:
        """
        Stream the single model's answer. Yields a StreamChunk per token
        (reasoning or content).

        If the connection drops *after* some tokens were already yielded,
        the error is re-raised as-is so the caller can keep what it got
        instead of throwing the partial answer away.
        """
        model = MODEL
        deadline = time.monotonic() + settings.ai_stream_max_duration
        try:
            async with httpx.AsyncClient(timeout=settings.ai_timeout) as client:
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                }
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise ModelRequestError(
                            model, f"HTTP {resp.status_code}: {body[:300]!r}"
                        )

                    line_iter = resp.aiter_lines()
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ModelRequestError(
                                model,
                                f"stream exceeded {settings.ai_stream_max_duration:.0f}s "
                                "total duration",
                            )
                        try:
                            line = await asyncio.wait_for(
                                line_iter.__anext__(), timeout=remaining
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise ModelRequestError(
                                model,
                                f"stream exceeded {settings.ai_stream_max_duration:.0f}s "
                                "total duration",
                            ) from None

                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        reasoning_piece = (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or ""
                        )
                        if reasoning_piece:
                            yield StreamChunk("reasoning", reasoning_piece)

                        content_piece = delta.get("content") or ""
                        if content_piece:
                            yield StreamChunk("content", content_piece)

        except ModelRequestError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to one error type
            logger.warning("Model %s failed: %s", model, exc)
            raise ModelRequestError(model, str(exc)) from exc

router = AIRouter()
