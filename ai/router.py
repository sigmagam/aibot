"""
Client for the AI API at https://9.todict.tech/v1 (OpenAI-compatible router).

Model selection flow:
    1. Try the given candidate list in order. Callers build this list with
       ai/models.py -> default_candidates() (plain messages: claude-opus-4-6-thinking,
       then grok-4.5-high) or provider_candidates() (the /ai provider+model
       picker: chosen model, then the rest of that provider, then everyone
       else).
    2. If every candidate in that list fails (connection error / 4xx / 5xx /
       timeout), the candidate list itself already ends with a shuffled
       fallback across the full catalog, so the loop below simply keeps
       going until one works or the list runs out.

Returns an async generator of StreamChunk so it can be used to stream
"thinking" + answer content to Telegram in real time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx

from ai.models import default_candidates
from config import settings

logger = logging.getLogger("ai.router")


@dataclass
class StreamChunk:
    """A single streamed chunk from the model."""
    kind: str  # "reasoning" | "content"
    delta: str


@dataclass
class GenerationResult:
    model_used: Optional[str] = None
    reasoning: str = ""
    content: str = ""
    attempts: list[str] = field(default_factory=list)
    error: Optional[str] = None


class AllModelsFailedError(Exception):
    def __init__(self, attempts: list[str]):
        self.attempts = attempts
        super().__init__(f"All candidate models failed: {', '.join(attempts)}")


class AIRouter:
    def __init__(self) -> None:
        self._base_url = settings.ai_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        }

    async def stream_chat(
        self,
        messages: list[dict],
        candidates: Optional[list[str]] = None,
    ) -> AsyncIterator[tuple[str, StreamChunk]]:
        """
        Generator that tries each candidate model until one succeeds.
        Yields (model_name, StreamChunk) for every chunk received.

        If `candidates` is omitted, falls back to the default priority +
        random order. If a model fails BEFORE any token came out at all,
        it automatically moves on to the next candidate. If some of the
        answer already streamed out and then the connection dropped, the
        error is re-raised as-is (so the caller can show what was
        received so far instead of silently swapping models mid-answer).
        """
        attempts: list[str] = []
        last_error: Optional[Exception] = None

        for model in candidates or default_candidates():
            attempts.append(model)
            got_any_chunk = False
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
                            raise RuntimeError(
                                f"HTTP {resp.status_code}: {body[:300]!r}"
                            )

                        line_iter = resp.aiter_lines()
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError(
                                    f"stream exceeded {settings.ai_stream_max_duration:.0f}s "
                                    "total duration"
                                )
                            try:
                                line = await asyncio.wait_for(
                                    line_iter.__anext__(), timeout=remaining
                                )
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                raise TimeoutError(
                                    f"stream exceeded {settings.ai_stream_max_duration:.0f}s "
                                    "total duration"
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
                                got_any_chunk = True
                                yield model, StreamChunk("reasoning", reasoning_piece)

                            content_piece = delta.get("content") or ""
                            if content_piece:
                                got_any_chunk = True
                                yield model, StreamChunk("content", content_piece)

                # Reached this point without an exception -> this model
                # succeeded, we're done.
                return

            except Exception as exc:  # noqa: BLE001 - intentionally broad for fallback
                logger.warning("Model %s failed: %s", model, exc)
                last_error = exc
                if got_any_chunk:
                    # Already streamed part of an answer to the caller,
                    # don't silently swap models mid-stream. This also
                    # covers the total-duration timeout above: whatever
                    # was received so far still gets finished off and
                    # sent (as a paste.rs link if it's long) instead of
                    # vanishing.
                    raise
                continue

        raise AllModelsFailedError(attempts) from last_error

    async def list_models(self) -> Optional[list[str]]:
        """Try to fetch the live model list from /models (if available)."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self._base_url}/models", headers=self._headers
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception:  # noqa: BLE001
            return None


router = AIRouter()
