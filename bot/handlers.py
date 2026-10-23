"""
All Kurigram handlers: /start, /help, /reset, /model, /ai, /sh, plain text
messages, and the small /start inline-button shortcuts.

Single-model bot: every prompt goes to the one model in AI_MODEL
(ai/models.py -> MODEL). There is no provider/model picker — plain messages
and `/ai <prompt>` both just run that model.

`/sh <command>` (admin only) is the multi-function part: it runs a shell
command on the VPS this bot is hosted on and posts the output back.

UI conventions used throughout this file:
  - All bot-authored messages use parse_mode=ParseMode.HTML so <b>,
    <i>, <code>, <pre>, and <blockquote> render properly instead of
    showing up as raw tags.
  - Inline buttons use the `style` kwarg (primary / success / danger)
    wherever the Kurigram/Pyrogram fork in use supports it, so the
    keyboard reads as color-coded at a glance:
        primary  (blue)  -> navigation / selection actions
        success  (green) -> confirm / pick / positive actions
        danger   (red)   -> back / cancel / destructive actions
"""
from __future__ import annotations
import inspect
import logging
import re
from collections import defaultdict, deque
import asyncio
import os
import subprocess
import sys

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

try:
    from pyrogram.enums import ButtonStyle
except ImportError:  # older fork without the enum
    ButtonStyle = None

from ai.models import MODEL
from ai.router import ModelRequestError, router
from bot.mentions import extract_prompt, should_respond
from bot.updater import sync as sync_repo
from config import settings
from database import (
    count_users,
    get_broadcast_targets,
    init_db,
    remove_user,
    upsert_user,
)
from telegram_rich.stream import StreamingReplier

logger = logging.getLogger("bot.handlers")

# Commands handled by this bot. Keep plain-text handler from catching commands.
_COMMANDS = [
    "start", "help", "reset", "model", "stats", "broadcast", "gitpull", "sh", "ai"
]

# Matches "/cmd@SomeBotUsername ..." so we can tell a command meant for a
# *different* bot apart from plain chat text — filters.command() only
# matches this bot's own username (or no username), so without this check
# "/start@OtherBot" in a group would fall through to on_text and get sent
# to the AI as if it were a normal prompt.
_COMMAND_FOR_OTHER_BOT_RE = re.compile(r"^/\w+@(\w+)", re.IGNORECASE)

HTML = ParseMode.HTML

# Telegram/Kurigram button colors need the real ButtonStyle enum, not a
# plain string — passing "primary" as a str is silently ignored by
# Pyrogram/Kurigram, which is why buttons stayed the default color.
_STYLE_MAP = {}
if ButtonStyle is not None:
    _STYLE_MAP = {
        "default": ButtonStyle.DEFAULT,
        "primary": ButtonStyle.PRIMARY,
        "danger": ButtonStyle.DANGER,
        "success": ButtonStyle.SUCCESS,
    }
_SUPPORTS_BUTTON_STYLE = "style" in inspect.signature(InlineKeyboardButton).parameters
_SUPPORTS_CUSTOM_EMOJI = "icon_custom_emoji_id" in inspect.signature(InlineKeyboardButton).parameters

# Matches a leading emoji (+ optional VS16 U+FE0F) followed by a space, e.g.
# "🧠 " or "⬅️ " at the start of a button label.
_LEADING_EMOJI_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]\uFE0F?\s+"
)


def _button(
    text: str,
    callback_data: str,
    style: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    """Build an inline button, applying a color style and/or a custom-emoji
    icon when the running Kurigram/Pyrogram fork supports them (falls back
    to a plain button otherwise, so this never crashes on older forks).
    When an icon is applied, any leading plain emoji + space already in
    `text` is stripped so the emoji doesn't show up twice (once as the
    custom-emoji icon, once as plain Unicode text)."""
    kwargs: dict = {}
    if style and _SUPPORTS_BUTTON_STYLE and style in _STYLE_MAP:
        kwargs["style"] = _STYLE_MAP[style]
    if icon_custom_emoji_id and _SUPPORTS_CUSTOM_EMOJI:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
        text = _LEADING_EMOJI_RE.sub("", text, count=1)
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


# Custom emoji IDs (real Telegram Premium custom emoji, "tg://emoji?id=...").
# icon_custom_emoji_id only renders for bots that bought an extra username on
# Fragment, or when the bot owner has Telegram Premium and DMs the button
# directly — so this is opt-in via ENABLE_BUTTON_ICONS, off by default so it
# never silently fails on bots that aren't eligible.
_BUTTON_ICONS = {
    "browse": "5372981976804366741",     # 🤖
    "reset": "4956591954088428445",      # 🧹
}
_ENABLE_BUTTON_ICONS = os.getenv("ENABLE_BUTTON_ICONS", "true").lower() == "true"


def _icon(key: str) -> str | None:
    return _BUTTON_ICONS.get(key) if _ENABLE_BUTTON_ICONS else None


def _esc(text: str) -> str:
    """Escape user-provided text before dropping it into an HTML message."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# Conversation history per chat_id, kept in memory (lost on process restart).
# Format: deque[{"role": "user"/"assistant", "content": str}]
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=settings.max_history))


def _build_messages(chat_id: int, prompt: str) -> list[dict]:
    messages = [{"role": "system", "content": settings.system_prompt}]
    messages.extend(_history[chat_id])
    messages.append({"role": "user", "content": prompt})
    return messages


def _run_shell(command: str) -> tuple[int | None, str]:
    """Run a shell command on the VPS (blocking — call it via
    `asyncio.to_thread`, see the /sh handler). Returns (exit_code, output)
    with stdout+stderr combined; exit_code is None if it hit SHELL_TIMEOUT."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=settings.shell_timeout,
            cwd=settings.shell_workdir or None,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return None, (
            partial.strip() + f"\n(timed out after {settings.shell_timeout:.0f}s)"
        ).strip()
    except Exception as exc:  # noqa: BLE001
        return -1, f"failed to run: {exc}"
    return proc.returncode, (proc.stdout or "").strip()


def _shell_report(command: str, code: int | None, output: str) -> str:
    if code is None:
        status = "⏱️ TIMEOUT"
    elif code == 0:
        status = "✅ exit 0"
    else:
        status = f"❌ exit {code}"
    body = output or "(no output)"
    if len(body) > 3500:
        body = body[:3500] + "\n... (truncated)"
    return (
        f"🖥️ <b>VPS SHELL</b> — {status}\n"
        f"<blockquote><code>{_esc(command)}</code></blockquote>\n"
        f"<pre>{_esc(body)}</pre>"
    )


async def _run_generation(
    client: Client,
    trigger_message: Message,
    prompt: str,
    *,
    placeholder: Message | None = None,
) -> None:
    chat_id = trigger_message.chat.id
    messages = _build_messages(chat_id, prompt)

    replier = StreamingReplier(client, trigger_message, placeholder=placeholder, show_reasoning=True)
    started = False
    try:
        async for chunk in router.stream_chat(messages):
            if not started:
                started = True
                await replier.start()
            if chunk.kind == "reasoning":
                await replier.push_reasoning(chunk.delta)
            else:
                await replier.push_content(chunk.delta)

        if not started:
            await trigger_message.reply_text(
                "⚠️ <b>No response</b>\n"
                "<blockquote>The model didn't return any answer. Please try again.</blockquote>",
                quote=True,
                parse_mode=HTML,
            )
            return

        await replier.finish(MODEL)

        _history[chat_id].append({"role": "user", "content": prompt})
        _history[chat_id].append({"role": "assistant", "content": replier.content})

    except ModelRequestError as exc:
        logger.error("Model request failed: %s", exc)
        if replier.content or getattr(replier, "_reasoning", ""):
            # Part of an answer already streamed out before the error hit
            # (e.g. a mid-stream drop or the total-duration timeout) —
            # finish it off normally (pasting to paste.rs if it's long)
            # instead of throwing the partial answer away.
            try:
                await replier.finish(MODEL)
                _history[chat_id].append({"role": "user", "content": prompt})
                _history[chat_id].append({"role": "assistant", "content": replier.content})
                return
            except Exception:
                logger.exception("Failed to finish partial answer after error")
        await replier.fail(
            "⚠️ <b>Model failed</b>\n"
            f"<blockquote><code>{_esc(MODEL)}</code> is currently failing "
            f"or unreachable.\n<code>{_esc(str(exc))}</code></blockquote>"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error while processing message")
        if replier.content or getattr(replier, "_reasoning", ""):
            try:
                await replier.finish(MODEL)
                _history[chat_id].append({"role": "user", "content": prompt})
                _history[chat_id].append({"role": "assistant", "content": replier.content})
                return
            except Exception:
                logger.exception("Failed to finish partial answer after error")
        await replier.fail(
            f"⚠️ <b>Unexpected error</b>\n<blockquote><code>{_esc(str(exc))}</code></blockquote>"
        )


def _model_info_text() -> str:
    return (
        "🧩 <b>MODEL</b>\n"
        f"✨ <code>{_esc(MODEL)}</code>\n\n"
        "<blockquote>One model, no picker. Send a normal message or use "
        "<code>/ai your prompt</code> — both go straight to it.</blockquote>"
    )


def register_handlers(app: Client) -> None:
    init_db()

    @app.on_message(filters.command("start"))
    async def start_cmd(client: Client, message: Message):
        upsert_user(message)

        # Message effects (Kurigram's `effect_id`, not the Bot-API-style
        # `message_effect_id`) only work in private chats — Telegram
        # rejects/ignores them in groups and channels, so only pass it
        # there.
        extra_kwargs: dict = {}
        if message.chat.type == ChatType.PRIVATE and settings.start_effect_id:
            extra_kwargs["effect_id"] = int(settings.start_effect_id)

        await message.reply_text(
            "╭────────────────────────╮\n"
            "        ✦ <b>AI STUDIO</b> ✦\n"
            "╰────────────────────────╯\n\n"
            "<blockquote>Welcome to your personal AI workspace. Chat, "
            "code, brainstorm — and run things on the VPS — straight "
            "from Telegram.</blockquote>\n\n"
            "⚡ <b>QUICK START</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• Send a normal message → the model replies\n"
            "• <code>/ai your prompt</code> → same model, explicit\n"
            "• <code>/sh command</code> → run a shell command on the VPS\n"
            "• <code>/model</code> → show the model in use\n"
            "• <code>/reset</code> → clear the current conversation\n\n"
            "💡 <b>PRO TIP</b>\n"
            "<i>Give the AI clear context, your goal, and the format "
            "you want for a better result.</i>\n\n"
            "🧩 <b>Example</b>\n"
            "<pre>/ai Explain quantum computing simply\n"
            "/sh df -h</pre>\n\n"
            "🚀 <b>Ready when you are.</b>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _button("🤖 Model info", "menu:model", style="primary", icon_custom_emoji_id=_icon("browse")),
                        _button("🧹 Reset chat", "menu:reset", style="danger", icon_custom_emoji_id=_icon("reset")),
                    ]
                ]
            ),
            **extra_kwargs,
        )

    @app.on_message(filters.command("help"))
    async def help_cmd(client: Client, message: Message):
        await start_cmd(client, message)

    @app.on_message(filters.command("reset"))
    async def reset_cmd(client: Client, message: Message):
        _history.pop(message.chat.id, None)
        await message.reply_text(
            "🧹 <b>Conversation cleared</b>\n"
            "<blockquote>History for this chat has been reset. Start fresh whenever you're ready.</blockquote>",
            parse_mode=HTML,
        )

    @app.on_message(filters.command("model"))
    async def model_cmd(client: Client, message: Message):
        await message.reply_text(_model_info_text(), parse_mode=HTML)

    def _is_admin(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)

    @app.on_message(filters.command("stats"))
    async def stats_cmd(client: Client, message: Message):
        if not _is_admin(message):
            return
        await message.reply_text(
            "📊 <b>BOT DATABASE</b>\n\n"
            f"👥 Users started: <b>{count_users()}</b>\n"
            f"🗃️ Storage: <code>{_esc(settings.database_path)}</code>",
            parse_mode=HTML,
        )

    @app.on_message(filters.command("broadcast"))
    async def broadcast_cmd(client: Client, message: Message):
        if not _is_admin(message):
            return
        if len(message.command) < 2 and not message.reply_to_message:
            await message.reply_text(
                "📣 <b>Broadcast</b>\n"
                "<blockquote>Use <code>/broadcast your message</code>, or reply to a "
                "message with <code>/broadcast</code> to forward it to every user.</blockquote>",
                parse_mode=HTML,
            )
            return

        targets = get_broadcast_targets()
        sent = failed = 0
        status = await message.reply_text(
            f"📣 <b>Preparing broadcast</b>\n"
            f"<blockquote>Sending to <b>{len(targets)}</b> users...</blockquote>",
            parse_mode=HTML,
        )

        for chat_id in targets:
            try:
                if message.reply_to_message:
                    await message.reply_to_message.copy(chat_id)
                else:
                    text = message.text.split(None, 1)[1].strip()
                    await client.send_message(chat_id, text, parse_mode=None)
                sent += 1
            except Exception as exc:
                failed += 1
                # A blocked/deleted chat should not be retried forever.
                if "USER_IS_BLOCKED" in str(exc) or "PEER_ID_INVALID" in str(exc):
                    remove_user(chat_id)
            await asyncio.sleep(0.05)

        await status.edit_text(
            "📣 <b>BROADCAST COMPLETE</b>\n\n"
            f"✅ Sent: <b>{sent}</b>\n"
            f"⚠️ Failed: <b>{failed}</b>\n"
            f"👥 Initial targets: <b>{len(targets)}</b>",
            parse_mode=HTML,
        )

    @app.on_message(filters.command("gitpull"))
    async def gitpull_cmd(client: Client, message: Message):
        """Pull the latest code from GitHub and restart. Pull only — this
        never commits or pushes anything."""
        if not _is_admin(message):
            return

        status = await message.reply_text(
            "🔄 <b>UPDATE DEPLOYMENT</b>\n"
            "<blockquote>Pulling the latest changes from GitHub...</blockquote>",
            parse_mode=HTML,
        )
        try:
            # Off the event loop: pip install can take minutes.
            ok, output = await asyncio.to_thread(
                sync_repo, settings.git_repo_dir, settings.git_branch
            )
        except Exception as exc:  # noqa: BLE001
            await status.edit_text(
                "❌ <b>UPDATE ERROR</b>\n\n" f"<pre>{_esc(str(exc)[-3500:])}</pre>",
                parse_mode=HTML,
            )
            return

        if not ok:
            await status.edit_text(
                "❌ <b>UPDATE FAILED</b>\n\n" f"<pre>{_esc(output[-3500:])}</pre>",
                parse_mode=HTML,
            )
            return

        await status.edit_text(
            "✅ <b>UPDATE SUCCESSFUL</b>\n\n"
            f"📌 {_esc(output)}\n"
            "♻️ Bot is restarting automatically...",
            parse_mode=HTML,
        )
        await asyncio.sleep(1)

        # Replace the current process so systemd/supervisor still sees
        # one bot process and the updated code is loaded immediately.
        repo = os.path.abspath(settings.git_repo_dir)
        os.chdir(repo)
        os.execv(sys.executable, [sys.executable, os.path.join(repo, "main.py")])

    @app.on_message(filters.command("sh"))
    async def sh_cmd(client: Client, message: Message):
        """Run a shell command on the VPS (admin only) — the bot as a
        remote terminal, not just a chat front-end."""
        if not _is_admin(message):
            return
        if not settings.enable_shell:
            await message.reply_text(
                "🚫 <b>Shell disabled</b>\n"
                "<blockquote>Set <code>ENABLE_SHELL=true</code> in .env and "
                "restart to enable it.</blockquote>",
                parse_mode=HTML,
            )
            return

        command = message.text.split(None, 1)[1].strip() if len(message.command) > 1 else ""
        if not command:
            await message.reply_text(
                "🖥️ <b>VPS SHELL</b>\n"
                "<blockquote>Run <code>/sh your command</code>, e.g. "
                "<code>/sh df -h</code>.</blockquote>",
                parse_mode=HTML,
            )
            return

        status = await message.reply_text(
            "🖥️ <b>Running</b>\n"
            f"<blockquote><code>{_esc(command)}</code></blockquote>",
            parse_mode=HTML,
        )
        # Off the event loop: the command can run for SHELL_TIMEOUT seconds.
        code, output = await asyncio.to_thread(_run_shell, command)
        await status.edit_text(
            _shell_report(command, code, output), parse_mode=HTML
        )

    @app.on_message(filters.command("ai"))
    async def ai_cmd(client: Client, message: Message):
        prompt = message.text.split(None, 1)[1].strip() if len(message.command) > 1 else ""
        if not prompt:
            await message.reply_text(
                "i️ <b>Usage</b>\n<pre>/ai your question</pre>",
                quote=True,
                parse_mode=HTML,
            )
            return
        await _run_generation(client, message, prompt)

    # Telegram Business / connected-account messages are delivered as
    # normal Message objects with `business_connection_id` set.
    # `filters.business` is the Kurigram filter for these updates.
    # Register this BEFORE the normal text handler so Business DMs are
    # explicitly routed through the AI pipeline.
    @app.on_message(filters.business & filters.text & ~filters.command(_COMMANDS))
    async def on_business_text(client: Client, message: Message):
        connection_id = getattr(message, "business_connection_id", None)
        if not connection_id:
            logger.warning("Received tg_business message without business_connection_id")
            return

        text = message.text or ""
        if not text.strip():
            return

        # Business chats are private conversations managed on behalf of the
        # connected account, so no @mention is required.
        prompt = extract_prompt(message, "")
        if not prompt:
            return

        logger.info(
            "Business message received: chat_id=%s connection_id=%s",
            message.chat.id,
            connection_id,
        )
        await _run_generation(client, message, prompt)

    @app.on_message(filters.text & ~filters.business & ~filters.command(_COMMANDS))
    async def on_text(client: Client, message: Message):
        me = client.me  # cached bot identity (username etc.), set by Kurigram at startup
        bot_username = me.username if me else ""

        text = message.text or ""
        other_bot_match = _COMMAND_FOR_OTHER_BOT_RE.match(text)
        if other_bot_match and other_bot_match.group(1).lower() != bot_username.lower():
            # e.g. "/start@SomeOtherBot" in a group chat with several bots —
            # not addressed to us, so don't treat it as a prompt.
            return

        if not should_respond(message, bot_username):
            # In groups: only mentions/replies-to-us trigger a response, so
            # the bot doesn't answer every message everyone else sends.
            return

        prompt = extract_prompt(message, bot_username)
        if not prompt:
            return

        # Plain message: run the single model immediately, no button.
        await _run_generation(client, message, prompt)

    @app.on_callback_query(filters.regex(r"^menu:"))
    async def on_menu_shortcut(client: Client, callback_query: CallbackQuery):
        """Shortcuts attached to /start buttons."""
        _, action = callback_query.data.split(":", 1)
        await callback_query.answer()
        if action == "model":
            await callback_query.message.reply_text(
                _model_info_text(), parse_mode=HTML
            )
        elif action == "reset":
            await reset_cmd(client, callback_query.message)
        elif action == "ai":
            await callback_query.message.reply_text(
                "i️ <b>Usage</b>\n<pre>/ai your question</pre>",
                parse_mode=HTML,
            )
