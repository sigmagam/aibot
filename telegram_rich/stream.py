"""
StreamingReplier: sends a model's answer to Telegram as it streams in,
periodically editing one message and auto-formatting Markdown -> HTML
(, , , , ).

When `show_reasoning=True` (the user picked "Thinking" mode), reasoning
tokens are accumulated and rendered inside a native Telegram *expandable*
blockquote (``) above the final answer, so people
can tap to see the model's reasoning without it cluttering the chat.
When `show_reasoning=False` ("Direct" mode), reasoning tokens are simply
discarded and only a "typing..." chat action is refreshed while we wait
for the first content token.
"""
from __future__ import annotations

import time

import httpx
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from config import settings
from telegram_rich.formatting import md_to_html

# Telegram's hard limit on a single message's text is 4096 UTF-16 code
# units. Once a streamed answer gets close to that, editing the message
# keeps failing (or gets silently truncated) — instead of spamming
# retries, the answer is cut off at _PASTE_THRESHOLD chars in-chat and the
# full text is uploaded to the paste API below, linked underneath.
_PASTE_THRESHOLD = 1500

_PASTE_API_BASE = "https://paster-lyart.vercel.app/api/paste"
_PASTE_TTL_MS = 24 * 60 * 60 * 1000  # 24h


async def _upload_to_pastebin(text: str) -> str | None:
    """POST raw text to the paste API and return the full paste URL, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.post(
                _PASTE_API_BASE,
                json={"text": text, "ttl": _PASTE_TTL_MS},
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            paste_id = data.get("id")
            if paste_id:
                return f"{_PASTE_API_BASE}?id={paste_id}"
    except Exception:
        pass
    return None


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
        self._over_threshold_notified = False

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
            parts.append(f"🧠 {md_to_html(self._reasoning)}")
        if self._content:
            parts.append(md_to_html(self._content))
        return "\n".join(parts) if parts else "…"

    async def _maybe_update(self, force: bool = False) -> None:
        if not self._content and not (self._show_reasoning and self._reasoning):
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < settings.stream_update_interval:
            return

        html_text = self._render()
        truncated = len(html_text) > _PASTE_THRESHOLD
        if truncated:
            # Keep showing the answer as it grows, just capped at the
            # threshold so edits don't keep failing/getting truncated by
            # Telegram while the model is still generating. finish() swaps
            # this for the real answer + paste link once the stream ends.
            html_text = html_text[:_PASTE_THRESHOLD] + "…"
        self._last_edit = now

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
            # HTML can be "incomplete" mid-stream (e.g. an unclosed  tag)
            # -> ignore, the next update will follow and is usually valid again.
            pass

    async def finish(self, model_used: str) -> None:
        html_text = self._render()

        if len(html_text) > _PASTE_THRESHOLD:
            # Too long — show the first _PASTE_THRESHOLD chars in-chat and
            # upload the full raw answer to the paste API, with the link
            # underneath instead of fighting Telegram's length limit or
            # splitting into multiple messages.
            paste_url = await _upload_to_pastebin(self._content or "(no answer)")
            preview = html_text[:_PASTE_THRESHOLD] + "…"
            if paste_url:
                note = (
                    f"{preview}\n\n"
                    f"📄 <b>Jawaban lengkap:</b> {paste_url}"
                )
            else:
                # Paste API itself failed — fall back to a hard-truncated
                # in-chat message rather than losing the answer entirely.
                note = preview + (
                    "\n\n<blockquote>⚠️ Jawaban dipotong — gagal upload "
                    "ke pastebin.</blockquote>"
                )
            if self._placeholder is not None:
                try:
                    await self._placeholder.edit_text(note, parse_mode=ParseMode.HTML, reply_markup=None)
                    return
                except Exception:
                    pass
            await self._trigger_message.reply_text(note, quote=True, parse_mode=ParseMode.HTML)
            return

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
