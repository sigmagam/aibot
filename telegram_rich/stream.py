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
# units. Once a streamed answer's rendered HTML gets close to that, editing
# the message keeps failing (or gets silently truncated) — instead of
# spamming retries, the full answer is uploaded to https://paste.rs and the
# chat only gets a short note + link. Kept comfortably under 4096 to leave
# room for the "too long, full answer:" note appended below it.
_TELEGRAM_MAX_LEN = 4096
_PASTE_THRESHOLD = 3500


async def _upload_to_pastebin(text: str) -> str | None:
    """POST raw text to paste.rs and return the paste URL, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.post("https://paste.rs/", content=text.encode("utf-8"))
        if resp.status_code in (201, 206):
            return resp.text.strip()
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
        if len(html_text) > _PASTE_THRESHOLD:
            # Already too long for a single Telegram message — stop editing
            # it every tick (those edits would just keep failing / getting
            # truncated). finish() will swap this to a paste.rs link once
            # the stream ends. Tell the user once so it doesn't just look
            # frozen while the model keeps generating in the background.
            if not self._over_threshold_notified and self._placeholder is not None:
                self._over_threshold_notified = True
                try:
                    await self._placeholder.edit_text(
                        "✍️ <b>Jawaban panjang, masih menyusun…</b>\n"
                        "<blockquote>Akan dikirim sebagai link begitu selesai.</blockquote>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            return
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

        if len(html_text) > _TELEGRAM_MAX_LEN:
            # Too long to fit in one Telegram message. Upload the raw
            # (un-rendered) answer to paste.rs and point the chat at the
            # link instead of fighting Telegram's length limit or
            # splitting into multiple messages.
            paste_url = await _upload_to_pastebin(self._content or "(no answer)")
            if paste_url:
                note = (
                    "📄 <b>Answer too long for Telegram</b>\n"
                    f"<blockquote>Full response: {paste_url}</blockquote>"
                )
            else:
                # paste.rs itself failed — fall back to a hard-truncated
                # in-chat message rather than losing the answer entirely.
                note = md_to_html(self._content[: _TELEGRAM_MAX_LEN - 200]) + (
                    "\n\n<blockquote>⚠️ Answer truncated — too long for "
                    "Telegram and the pastebin upload failed.</blockquote>"
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
