"""
StreamingReplier: sends a model's answer to Telegram as it streams in,
periodically editing one message and auto-formatting Markdown -> HTML
(<b>, <i>, <code>, <pre>, <blockquote>).

When `show_reasoning=True` (the user picked "Thinking" mode), reasoning
tokens are accumulated and rendered inside a native Telegram *expandable*
blockquote (`<blockquote expandable>`) above the final answer, so people
can tap to see the model's reasoning without it cluttering the chat.
When `show_reasoning=False` ("Direct" mode), reasoning tokens are simply
discarded and only a "typing..." chat action is refreshed while we wait
for the first content token.
"""
from __future__ import annotations

import time

from pyrogram.enums import ChatAction, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from config import settings
from telegram_rich.formatting import md_to_html


class StreamingReplier:
    def __init__(
        self,
        client,
        trigger_message: Message,
        placeholder: Message | None = None,
        show_reasoning: bool = False,
    ) -> None:
        self._client = client
        self._trigger_message = trigger_message
        self._chat_id = trigger_message.chat.id
        self._placeholder = placeholder
        self._show_reasoning = show_reasoning
        self._reasoning = ""
        self._content = ""
        self._last_edit = 0.0

    @property
    def content(self) -> str:
        return self._content

    async def start(self) -> None:
        try:
            await self._client.send_chat_action(self._chat_id, ChatAction.TYPING)
        except Exception:
            pass

    async def push_reasoning(self, delta: str) -> None:
        if self._show_reasoning:
            self._reasoning += delta
            await self._maybe_update()
            return
        # Reasoning mode is off: don't show it, just keep the "typing"
        # indicator alive while we wait for the first content token.
        if not self._content:
            try:
                await self._client.send_chat_action(self._chat_id, ChatAction.TYPING)
            except Exception:
                pass

    async def push_content(self, delta: str) -> None:
        self._content += delta
        await self._maybe_update()

    def _render(self) -> str:
        parts: list[str] = []
        if self._show_reasoning and self._reasoning:
            parts.append(f"<blockquote expandable>🧠 {md_to_html(self._reasoning)}</blockquote>")
        if self._content:
            parts.append(md_to_html(self._content))
        return "\n".join(parts) if parts else "…"

    async def _maybe_update(self, force: bool = False) -> None:
        if not self._content and not (self._show_reasoning and self._reasoning):
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < settings.stream_update_interval:
            return
        self._last_edit = now

        html_text = self._render()

        if self._placeholder is None:
            self._placeholder = await self._trigger_message.reply_text(
                html_text, quote=True, parse_mode=ParseMode.HTML
            )
            return
        try:
            await self._placeholder.edit_text(
                html_text, parse_mode=ParseMode.HTML, reply_markup=None
            )
        except MessageNotModified:
            pass
        except Exception:
            # HTML can be "incomplete" mid-stream (e.g. an unclosed <b> tag)
            # -> ignore, the next update will follow and is usually valid again.
            pass

    async def finish(self, model_used: str) -> None:
        await self._maybe_update(force=True)
        if self._placeholder is None:
            text = self._content or "(no answer)"
            await self._trigger_message.reply_text(
                md_to_html(text) if self._content else text,
                quote=True,
                parse_mode=ParseMode.HTML if self._content else None,
            )

    async def fail(self, error_text: str) -> None:
        text = f"⚠️ {error_text}"
        if self._placeholder is not None:
            try:
                await self._placeholder.edit_text(text, parse_mode=None, reply_markup=None)
                return
            except Exception:
                pass
        await self._trigger_message.reply_text(text, quote=True, parse_mode=None)
