"""
StreamingReplier: sends a model's answer to Telegram as it streams in,
periodically editing one message and auto-formatting Markdown -> HTML.

Layout of the single reply message (everything in ONE message — nothing is
ever split into a second bubble):

    🧠 [reasoning]                 <- collapsed expandable blockquote, tail only
    🔧 ls -lah /root               <- collapsed expandable blockquote per command
    🔧 df -h
    <the answer>

In agent mode (ai/agent.py) the model may run several shell commands before
it answers; those show up as the 🔧 blocks so you can see it is really
working on the box. Reasoning and tool output are kept short and collapsed,
so the answer itself always fits in the message instead of being cut off
and pushed to paste.rs.

Text is kept tidy on purpose: reasoning streams arrive as many small
fragments (sometimes missing spaces between sentences), so the reasoning
tail is whitespace-normalised and re-joined before rendering.
"""
from __future__ import annotations

import html
import re
import time

import httpx
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from config import settings
from telegram_rich.formatting import md_to_html

# Telegram's hard limit on a single message's text is 4096 UTF-16 code
# units. Only a genuinely huge answer is cut off in-chat and uploaded to
# paste.rs, linked underneath.
_PASTE_THRESHOLD = 3900

# Collapsed-detail budgets: how much of the reasoning / tool output stays
# visible inside the message. Small on purpose — the answer is the point.
_REASONING_TAIL = 600
_MAX_TOOL_ENTRIES = 3
_TOOL_COMMAND_CHARS = 100
_TOOL_OUTPUT_CHARS = 250
_MAX_INTERIM_CHARS = 160

# Reasoning fragments often run together ("...check it.The next step..."),
# so a period/!/? followed directly by a capital gets a space back.
_GLUED_SENTENCE_RE = re.compile(r"(?<=[a-z0-9\)\"'\]])\.(?=[A-Z])")


def _tidy(text: str) -> str:
    """Whitespace-normalise a streamed text fragment without losing
    paragraph breaks."""
    text = _GLUED_SENTENCE_RE.sub(". ", text)
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_tags(text: str) -> str:
    """Plain-text version of a rendered message, used as a last-resort
    fallback when Telegram rejects the HTML (e.g. a tag was cut in half by
    truncation). Keeps everything in the same message."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(blockquote|pre|code|b|i)>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


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
        # Business chats require all outbound operations to carry the
        # connection ID received with the inbound business message.
        self._business_connection_id = getattr(trigger_message, "business_connection_id", None)
        self._placeholder = placeholder
        self._show_reasoning = show_reasoning
        self._reasoning = ""
        self._content = ""
        self._interim: list[str] = []
        self._tools: list[str] = []
        self._last_edit = 0.0
        self._over_threshold_notified = False

    @property
    def content(self) -> str:
        return self._content

    async def start(self) -> None:
        try:
            await self._client.send_chat_action(
                self._chat_id,
                ChatAction.TYPING,
                **({"business_connection_id": self._business_connection_id}
                   if self._business_connection_id else {}),
            )
        except Exception:
            pass

    async def push_reasoning(self, delta: str) -> None:
        if self._show_reasoning:
            self._reasoning += delta
            await self._maybe_update()
            return
        # Reasoning display is off: just keep the "typing" indicator alive
        # while we wait for the first content token.
        if not self._content:
            try:
                await self._client.send_chat_action(
                    self._chat_id,
                    ChatAction.TYPING,
                    **({"business_connection_id": self._business_connection_id}
                       if self._business_connection_id else {}),
                )
            except Exception:
                pass

    async def push_content(self, delta: str) -> None:
        self._content += delta
        await self._maybe_update()

    async def push_tool(self, command: str, code: int | None, output: str) -> None:
        """Record one shell command the agent ran (agent mode).

        Anything the model wrote before deciding to run a command is folded
        into a compact italic note, so the final message shows the answer
        instead of a step-by-step monologue.
        """
        if self._content.strip():
            self._interim.append(_clip(self._content, _MAX_INTERIM_CHARS))
            self._content = ""

        if code is None:
            status = "timeout"
        elif code == 0:
            status = "exit 0"
        else:
            status = f"exit {code}"

        self._tools.append(
            f"🔧 <code>{html.escape(_clip(command, _TOOL_COMMAND_CHARS))}</code> · <i>{status}</i>\n"
            f"<blockquote expandable><pre>{html.escape(_clip(output or '(no output)', _TOOL_OUTPUT_CHARS))}</pre></blockquote>"
        )
        while len(self._tools) > _MAX_TOOL_ENTRIES:
            self._tools.pop(0)
        await self._maybe_update(force=True)

    def _render(self) -> str:
        parts: list[str] = []

        if self._show_reasoning and self._reasoning.strip():
            tail = _tidy(self._reasoning)[-_REASONING_TAIL:]
            parts.append(
                f"<blockquote expandable>🧠 <i>{md_to_html(tail)}</i></blockquote>"
            )

        if self._interim:
            parts.append("\n".join(f"<i>{md_to_html(note)}</i>" for note in self._interim))

        if self._tools:
            parts.append("\n".join(self._tools))

        if self._content.strip():
            parts.append(md_to_html(self._content))

        return "\n".join(parts) if parts else "..."

    async def _maybe_update(self, force: bool = False) -> None:
        if not self._content and not self._tools and not (
            self._show_reasoning and self._reasoning
        ):
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < settings.stream_update_interval:
            return

        html_text = self._render()
        if len(html_text) > _PASTE_THRESHOLD:
            # Never let a long tool/reasoning tail push the answer out of the
            # message: drop the oldest details first, the answer stays.
            self._tools = self._tools[-1:]
            self._interim = self._interim[-1:]
            html_text = self._render()
            if len(html_text) > _PASTE_THRESHOLD:
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
            # HTML can be "incomplete" mid-stream (e.g. an unclosed tag) and
            # Telegram edits can race; the next update usually fixes it.
            pass

    async def finish(self, model_used: str) -> None:
        html_text = self._render()

        if len(html_text) > _PASTE_THRESHOLD:
            # Really long answer: keep the head in-chat and link the full
            # text, instead of splitting into a second message.
            paste_url = await _upload_to_pastebin(self._content or "(no answer)")
            preview = html_text[:_PASTE_THRESHOLD] + "…"
            if paste_url:
                note = f"{preview}\n\n📄 <b>Jawaban lengkap:</b> {paste_url}"
            else:
                note = preview + (
                    "\n\n<blockquote>⚠️ Jawaban dipotong — gagal upload "
                    "ke pastebin.</blockquote>"
                )
            if self._placeholder is not None:
                try:
                    await self._placeholder.edit_text(note, parse_mode=ParseMode.HTML, reply_markup=None)
                    return
                except Exception:
                    # Truncated HTML can be invalid -> same message, plain text.
                    try:
                        await self._placeholder.edit_text(_strip_tags(note), reply_markup=None)
                        return
                    except Exception:
                        pass
            await self._trigger_message.reply_text(_strip_tags(note), quote=True)
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
