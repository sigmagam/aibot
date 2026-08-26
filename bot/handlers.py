"""
All Kurigram handlers: /start, /help, /reset, /model, /ai, plain text
messages, and the inline-button flows for both.

Two separate flows:
  - Plain message (typed directly, mention, or reply-to-bot): no button,
    runs immediately with ai/models.py -> default_candidates() (tries
    claude-opus-4-6-thinking, then grok-4.5-high, then random fallback).
  - /ai <prompt>: shows a provider picker button first (Antigravity,
    Gemini CLI, Groq, ...). After picking a provider, shows a model
    picker for that provider. After picking a model, runs with
    ai/models.py -> provider_candidates() (chosen model first, then the
    rest of that provider, then a random fallback across every other
    provider if the whole chosen provider fails).

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
import time
import uuid
from collections import defaultdict, deque
import asyncio
import os
import subprocess
import sys

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
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

from ai.models import (
    MODEL_CATALOG,
    MODEL_PRIORITY,
    PROVIDER_LABELS,
    default_candidates,
    provider_candidates,
)
from ai.router import AllModelsFailedError, router
from config import settings
from database import (
    count_users,
    get_broadcast_targets,
    init_db,
    remove_user,
    upsert_user,
)
from bot.mentions import extract_prompt
from telegram_rich.stream import StreamingReplier

logger = logging.getLogger("bot.handlers")

# Commands handled by this bot. Keep plain-text handler from catching commands.
_COMMANDS = [
    "start", "help", "reset", "model", "stats", "broadcast", "gitpull", "ai"
]

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


def _button(
    text: str,
    callback_data: str,
    style: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    """Build an inline button, applying a color style and/or a custom-emoji
    icon when the running Kurigram/Pyrogram fork supports them (falls back
    to a plain button otherwise, so this never crashes on older forks)."""
    kwargs: dict = {}
    if style and _SUPPORTS_BUTTON_STYLE and style in _STYLE_MAP:
        kwargs["style"] = _STYLE_MAP[style]
    if icon_custom_emoji_id and _SUPPORTS_CUSTOM_EMOJI:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


# Custom emoji IDs (real Telegram Premium custom emoji, "tg://emoji?id=...").
# icon_custom_emoji_id only renders for bots that bought an extra username on
# Fragment, or when the bot owner has Telegram Premium and DMs the button
# directly — so this is opt-in via ENABLE_BUTTON_ICONS, off by default so it
# never silently fails on bots that aren't eligible.
_BUTTON_ICONS = {
    "provider": "5237799019329105246",   # 🧠
    "model": "5472164874886846699",      # ✨
    "cancel": "5337249876126215335",     # ❌
    "back": "5258236805890710909",       # ⬅️
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

# Prompts waiting on an /ai provider/model button tap, keyed by a short
# random token referenced from the button's callback_data. Cleared once
# used, or pruned once they get too old (see _prune_pending).
_pending: dict[str, dict] = {}


def _prune_pending() -> None:
    cutoff = time.monotonic() - settings.pending_ttl
    stale = [token for token, item in _pending.items() if item["created_at"] < cutoff]
    for token in stale:
        _pending.pop(token, None)


def _build_messages(chat_id: int, prompt: str) -> list[dict]:
    messages = [{"role": "system", "content": settings.system_prompt}]
    messages.extend(_history[chat_id])
    messages.append({"role": "user", "content": prompt})
    return messages


async def _run_generation(
    client: Client,
    trigger_message: Message,
    prompt: str,
    *,
    candidates: list[str],
    placeholder: Message | None = None,
) -> None:
    chat_id = trigger_message.chat.id
    messages = _build_messages(chat_id, prompt)

    replier = StreamingReplier(client, trigger_message, placeholder=placeholder, show_reasoning=True)
    started = False
    model_used = None
    try:
        async for model, chunk in router.stream_chat(messages, candidates=candidates):
            if not started:
                started = True
                model_used = model
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

        await replier.finish(model_used)

        _history[chat_id].append({"role": "user", "content": prompt})
        _history[chat_id].append({"role": "assistant", "content": replier.content})

    except AllModelsFailedError as exc:
        logger.error("All candidate models failed: %s", exc)
        err_text = (
            "⚠️ <b>All models failed</b>\n"
            "<blockquote>Every candidate model is currently failing or unreachable.\n"
            f"Tried: <code>{_esc(', '.join(exc.attempts))}</code></blockquote>"
        )
        await replier.fail(err_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error while processing message")
        await replier.fail(
            f"⚠️ <b>Unexpected error</b>\n<blockquote><code>{_esc(str(exc))}</code></blockquote>"
        )


def _provider_keyboard(token: str) -> InlineKeyboardMarkup:
    providers = list(MODEL_CATALOG.keys())
    rows = []
    for i in range(0, len(providers), 2):
        row = []
        for provider in providers[i : i + 2]:
            label = PROVIDER_LABELS.get(provider, provider)
            row.append(_button(f"🧠 {label}", f"prov:{provider}:{token}", style="primary", icon_custom_emoji_id=_icon("provider")))
        rows.append(row)
    rows.append([_button("❌ Cancel", f"cancel:{token}", style="danger", icon_custom_emoji_id=_icon("cancel"))])
    return InlineKeyboardMarkup(rows)


def _model_keyboard(provider: str, token: str) -> InlineKeyboardMarkup:
    models = MODEL_CATALOG.get(provider, [])
    rows = []
    for i in range(0, len(models), 2):
        row = []
        for model in models[i : i + 2]:
            # short_name = last path segment, e.g. "cf/@cf/meta/llama-3.2-1b-instruct" -> "llama-3.2-1b-instruct"
            short_name = model.rsplit("/", 1)[-1]
            row.append(_button(f"✨ {short_name}", f"model:{model}:{token}", style="success", icon_custom_emoji_id=_icon("model")))
        rows.append(row)
    rows.append([_button("⬅️ Back to providers", f"back:{token}", style="danger", icon_custom_emoji_id=_icon("back"))])
    return InlineKeyboardMarkup(rows)


async def _offer_provider_selection(message: Message, prompt: str) -> None:
    """/ai flow: ask which provider to use before calling the AI."""
    _prune_pending()
    token = uuid.uuid4().hex[:8]
    _pending[token] = {
        "chat_id": message.chat.id,
        "prompt": prompt,
        "trigger_message": message,
        "created_at": time.monotonic(),
    }
    await message.reply_text(
        "🧭 <b>Choose a provider</b>\n"
        "<blockquote>Pick which AI provider should answer your prompt.</blockquote>",
        quote=True,
        parse_mode=HTML,
        reply_markup=_provider_keyboard(token),
    )


def register_handlers(app: Client) -> None:
    init_db()

    @app.on_message(filters.command("start"))
    async def start_cmd(client: Client, message: Message):
        upsert_user(message)
        await message.reply_text(
            "╭────────────────────────╮\n"
            "        ✦ <b>AI STUDIO</b> ✦\n"
            "╰────────────────────────╯\n\n"
            "<blockquote>Welcome to your personal AI workspace. "
            "Chat, code, brainstorm, and explore multiple AI models "
            "directly from Telegram.</blockquote>\n\n"
            "⚡ <b>QUICK START</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• Send a normal message → automatic model selection\n"
            "• <code>/ai your prompt</code> → choose provider &amp; model\n"
            "• <code>/model</code> → browse all available models\n"
            "• <code>/reset</code> → clear the current conversation\n\n"
            "💡 <b>PRO TIP</b>\n"
            "<i>Give the AI clear context, your goal, and the format "
            "you want for a better result.</i>\n\n"
            "🧩 <b>Example</b>\n"
            "<pre>/ai Explain quantum computing simply</pre>\n\n"
            "🚀 <b>Ready when you are.</b>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _button("🤖 Browse models", "menu:model", style="primary", icon_custom_emoji_id=_icon("browse")),
                        _button("🧹 Reset chat", "menu:reset", style="danger", icon_custom_emoji_id=_icon("reset")),
                    ]
                ]
            ),
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
        total = sum(len(models) for models in MODEL_CATALOG.values())
        lines = [
            "🧩 <b>MODEL DIRECTORY</b>",
            "",
            f"📦 <b>{total}</b> models available across "
            f"<b>{len(MODEL_CATALOG)}</b> providers.",
            "",
            "🤖 <b>Auto mode priority</b>",
        ]
        for i, m in enumerate(MODEL_PRIORITY, start=1):
            lines.append(f"  {i}. <code>{_esc(m)}</code>")
        lines += ["", "📚 <b>Providers &amp; models</b>"]
        for provider, models in MODEL_CATALOG.items():
            label = PROVIDER_LABELS.get(provider, provider)
            lines.append(f"\n▸ <b>{_esc(label)}</b> ({len(models)} models)")
            for m in models:
                lines.append(f"  • <code>{_esc(m)}</code>")
        lines += [
            "",
            "<blockquote>🎛️ Use <code>/ai your prompt</code> to pick "
            "a provider and model manually.</blockquote>",
        ]
        await message.reply_text(
            "\n".join(lines),
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup(
                [[_button("🧭 Pick provider &amp; model", "menu:ai", style="primary", icon_custom_emoji_id=_icon("provider"))]]
            ),
        )

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
            f"<blockquote>Sending to <b>{len(targets)}</b> users…</blockquote>",
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
        if not _is_admin(message):
            return

        status = await message.reply_text(
            "🔄 <b>UPDATE DEPLOYMENT</b>\n"
            "<blockquote>Pulling the latest changes from GitHub…</blockquote>",
            parse_mode=HTML,
        )
        try:
            repo = os.path.abspath(settings.git_repo_dir)
            pull = subprocess.run(
                ["git", "-C", repo, "pull", "--ff-only", "origin", settings.git_branch],
                capture_output=True, text=True, timeout=120,
            )
            if pull.returncode != 0:
                output = (pull.stderr or pull.stdout or "git pull failed").strip()
                await status.edit_text(
                    "❌ <b>UPDATE FAILED</b>\n\n"
                    f"<pre>{_esc(output[-3500:])}</pre>",
                    parse_mode=HTML,
                )
                return

            req = os.path.join(repo, "requirements.txt")
            if os.path.exists(req):
                pip = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
                    capture_output=True, text=True, timeout=180,
                )
                if pip.returncode != 0:
                    output = (pip.stderr or pip.stdout or "pip install failed").strip()
                    await status.edit_text(
                        "⚠️ <b>CODE UPDATED, DEPENDENCY INSTALL FAILED</b>\n\n"
                        f"<pre>{_esc(output[-3500:])}</pre>",
                        parse_mode=HTML,
                    )
                    return

            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "unknown"

            await status.edit_text(
                "✅ <b>UPDATE SUCCESSFUL</b>\n\n"
                f"📌 Commit: <code>{_esc(commit)}</code>\n"
                "♻️ Bot is restarting automatically…",
                parse_mode=HTML,
            )
            await asyncio.sleep(1)

            # Replace the current process so systemd/supervisor still sees
            # one bot process and the updated code is loaded immediately.
            os.chdir(repo)
            os.execv(sys.executable, [sys.executable, os.path.join(repo, "main.py")])

        except Exception as exc:
            await status.edit_text(
                "❌ <b>UPDATE ERROR</b>\n\n"
                f"<pre>{_esc(str(exc)[-3500:])}</pre>",
                parse_mode=HTML,
            )

    @app.on_message(filters.command("ai"))
    async def ai_cmd(client: Client, message: Message):
        prompt = message.text.split(None, 1)[1].strip() if len(message.command) > 1 else ""
        if not prompt:
            await message.reply_text(
                "ℹ️ <b>Usage</b>\n<pre>/ai your question</pre>",
                quote=True,
                parse_mode=HTML,
            )
            return
        await _offer_provider_selection(message, prompt)

    @app.on_message(filters.text & ~filters.command(_COMMANDS))
    async def on_text(client: Client, message: Message):
        me = client.me  # cached bot identity (username etc.), set by Kurigram at startup
        bot_username = me.username if me else ""

        prompt = extract_prompt(message, bot_username)
        if not prompt:
            return

        # Plain message: no button, run immediately with the default order.
        await _run_generation(client, message, prompt, candidates=default_candidates())

    @app.on_callback_query(filters.regex(r"^menu:"))
    async def on_menu_shortcut(client: Client, callback_query: CallbackQuery):
        """Shortcuts attached to /start and /model buttons."""
        _, action = callback_query.data.split(":", 1)
        await callback_query.answer()
        if action == "model":
            await model_cmd(client, callback_query.message)
        elif action == "reset":
            await reset_cmd(client, callback_query.message)
        elif action == "ai":
            await callback_query.message.reply_text(
                "ℹ️ <b>Usage</b>\n<pre>/ai your question</pre>",
                parse_mode=HTML,
            )

    @app.on_callback_query(filters.regex(r"^prov:"))
    async def on_provider_chosen(client: Client, callback_query: CallbackQuery):
        _, provider, token = callback_query.data.split(":", 2)
        pending = _pending.get(token)

        if pending is None:
            await callback_query.answer(
                "This request expired — please send /ai again.", show_alert=True
            )
            return

        await callback_query.answer()
        pending["provider"] = provider
        try:
            await callback_query.message.edit_text(
                f"🧭 <b>Provider:</b> {_esc(PROVIDER_LABELS.get(provider, provider))}\n"
                "<blockquote>Now pick a model:</blockquote>",
                parse_mode=HTML,
                reply_markup=_model_keyboard(provider, token),
            )
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^back:"))
    async def on_back_to_providers(client: Client, callback_query: CallbackQuery):
        _, token = callback_query.data.split(":", 1)
        pending = _pending.get(token)

        if pending is None:
            await callback_query.answer(
                "This request expired — please send /ai again.", show_alert=True
            )
            return

        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                "🧭 <b>Choose a provider</b>\n"
                "<blockquote>Pick which AI provider should answer your prompt.</blockquote>",
                parse_mode=HTML,
                reply_markup=_provider_keyboard(token),
            )
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^cancel:"))
    async def on_cancel(client: Client, callback_query: CallbackQuery):
        _, token = callback_query.data.split(":", 1)
        _pending.pop(token, None)
        await callback_query.answer("Cancelled.")
        try:
            await callback_query.message.edit_text(
                "✖️ <b>Cancelled</b>\n<blockquote>No model was run for that prompt.</blockquote>",
                parse_mode=HTML,
            )
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^model:"))
    async def on_model_chosen(client: Client, callback_query: CallbackQuery):
        _, model, token = callback_query.data.split(":", 2)
        pending = _pending.pop(token, None)

        if pending is None:
            await callback_query.answer(
                "This request expired — please send /ai again.", show_alert=True
            )
            return

        await callback_query.answer()
        try:
            await callback_query.message.edit_reply_markup(None)
        except Exception:
            pass

        provider = pending.get("provider") or ""
        candidates = provider_candidates(provider, model)

        await _run_generation(
            client,
            pending["trigger_message"],
            pending["prompt"],
            candidates=candidates,
            placeholder=callback_query.message,
        )
