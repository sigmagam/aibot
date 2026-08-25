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
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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

# Telegram Bot API supports button styles as strings: primary, success, danger.
# Keep this compatible with Kurigram/Pyrogram forks without requiring a
# ButtonStyle enum that may not exist in the installed version.
try:
    from pyrogram.types import InlineKeyboardButton as _IKB
    _SUPPORTS_BUTTON_STYLE = "style" in inspect.signature(_IKB).parameters
except (ImportError, TypeError, ValueError):
    _SUPPORTS_BUTTON_STYLE = False


def _button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    kwargs = {"style": style} if (style and _SUPPORTS_BUTTON_STYLE) else {}
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)

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
                "⚠️ The model didn't return any answer.", quote=True, parse_mode=None
            )
            return

        await replier.finish(model_used)

        _history[chat_id].append({"role": "user", "content": prompt})
        _history[chat_id].append({"role": "assistant", "content": replier.content})

    except AllModelsFailedError as exc:
        logger.error("All candidate models failed: %s", exc)
        err_text = (
            "⚠️ All AI models are currently failing / unreachable.\n"
            f"Models tried: {', '.join(exc.attempts)}"
        )
        await replier.fail(err_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error while processing message")
        await replier.fail(f"Unexpected error: {exc}")


def _provider_keyboard(token: str) -> InlineKeyboardMarkup:
    providers = list(MODEL_CATALOG.keys())
    rows = []
    for i in range(0, len(providers), 2):
        row = []
        for provider in providers[i : i + 2]:
            label = PROVIDER_LABELS.get(provider, provider)
            row.append(_button(label, f"prov:{provider}:{token}", style="primary"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _model_keyboard(provider: str, token: str) -> InlineKeyboardMarkup:
    models = MODEL_CATALOG.get(provider, [])
    rows = []
    for i in range(0, len(models), 2):
        row = []
        for model in models[i : i + 2]:
            # short_name = last path segment, e.g. "cf/@cf/meta/llama-3.2-1b-instruct" -> "llama-3.2-1b-instruct"
            short_name = model.rsplit("/", 1)[-1]
            row.append(_button(short_name, f"model:{model}:{token}", style="success"))
        rows.append(row)
    rows.append([_button("⬅️ Back to providers", f"back:{token}", style="danger")])
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
        "Pick a provider:",
        quote=True,
        parse_mode=None,
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
            "• <code>/ai your prompt</code> → choose provider & model\n"
            "• <code>/model</code> → browse all available models\n"
            "• <code>/reset</code> → clear the current conversation\n\n"
            "💡 <b>PRO TIP</b>\n"
            "<i>Give the AI clear context, your goal, and the format "
            "you want for a better result.</i>\n\n"
            "🧩 <b>Example</b>\n"
            "<pre>/ai Explain quantum computing simply</pre>\n\n"
            "🚀 <b>Ready when you are.</b>"
        )

    @app.on_message(filters.command("help"))
    async def help_cmd(client: Client, message: Message):
        await start_cmd(client, message)

    @app.on_message(filters.command("reset"))
    async def reset_cmd(client: Client, message: Message):
        _history.pop(message.chat.id, None)
        await message.reply_text("Chat history for this chat has been cleared.", parse_mode=None)

    @app.on_message(filters.command("model"))
    async def model_cmd(client: Client, message: Message):
        total = sum(len(models) for models in MODEL_CATALOG.values())
        lines = [
            "🧩 MODEL DIRECTORY",
            "",
            f"📦 {total} model tersedia dari "
            f"{len(MODEL_CATALOG)} provider.",
            "",
            "🤖 Auto mode",
        ]
        for i, m in enumerate(MODEL_PRIORITY, start=1):
            lines.append(f"  {i}. {m}")
        lines += ["", "📚 Daftar provider & model"]
        for provider, models in MODEL_CATALOG.items():
            label = PROVIDER_LABELS.get(provider, provider)
            lines.append(f"\n▸ {label} ({len(models)} model)")
            for m in models:
                lines.append(f"  • {m}")
        lines += [
            "",
            "🎛️ Gunakan /ai prompt untuk memilih provider "
            "dan model secara manual."
        ]
        await message.reply_text("\n".join(lines), parse_mode=None)

    def _is_admin(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)

    @app.on_message(filters.command("stats"))
    async def stats_cmd(client: Client, message: Message):
        if not _is_admin(message):
            return
        await message.reply_text(
            f"📊 BOT DATABASE\n\n"
            f"👥 Users started: {count_users()}\n"
            f"🗃️ Storage: {settings.database_path}",
            parse_mode=None,
        )

    @app.on_message(filters.command("broadcast"))
    async def broadcast_cmd(client: Client, message: Message):
        if not _is_admin(message):
            return
        if len(message.command) < 2 and not message.reply_to_message:
            await message.reply_text(
                "📣 Broadcast\n\n"
                "Gunakan /broadcast pesan atau reply sebuah pesan "
                "lalu kirim /broadcast.",
                parse_mode=None,
            )
            return

        targets = get_broadcast_targets()
        sent = failed = 0
        status = await message.reply_text(
            f"📣 Menyiapkan broadcast ke {len(targets)} users…",
            parse_mode=None,
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
            "📣 BROADCAST SELESAI\n\n"
            f"✅ Sent: {sent}\n"
            f"⚠️ Failed: {failed}\n"
            f"👥 Initial targets: {len(targets)}",
            parse_mode=None,
        )

    @app.on_message(filters.command("gitpull"))
    async def gitpull_cmd(client: Client, message: Message):
        if not _is_admin(message):
            return

        status = await message.reply_text(
            "🔄 UPDATE DEPLOYMENT\n\n"
            "Pulling the latest changes from GitHub…",
            parse_mode=None,
        )
        try:
            repo = os.path.abspath(settings.git_repo_dir)
            pull = subprocess.run(
                ["git", "-C", repo, "pull", "--ff-only", "origin", settings.git_branch],
                capture_output=True, text=True, timeout=120,
            )
            if pull.returncode != 0:
                output = (pull.stderr or pull.stdout or "git pull gagal").strip()
                await status.edit_text(
                    "❌ UPDATE FAILED\n\n"
                    + output[-3500:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    + "",
                    parse_mode=None,
                )
                return

            req = os.path.join(repo, "requirements.txt")
            if os.path.exists(req):
                pip = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
                    capture_output=True, text=True, timeout=180,
                )
                if pip.returncode != 0:
                    output = (pip.stderr or pip.stdout or "pip install gagal").strip()
                    await status.edit_text(
                        "⚠️ CODE UPDATED, DEPENDENCY INSTALL FAILED\n\n"
                        + output[-3500:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        + "",
                        parse_mode=None,
                    )
                    return

            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "unknown"

            await status.edit_text(
                "✅ UPDATE BERHASIL\n\n"
                f"📌 Commit: {commit}\n"
                "♻️ Bot is restarting automatically…",
                parse_mode=None,
            )
            await asyncio.sleep(1)

            # Replace the current process so systemd/supervisor still sees
            # one bot process and the updated code is loaded immediately.
            os.chdir(repo)
            os.execv(sys.executable, [sys.executable, os.path.join(repo, "main.py")])

        except Exception as exc:
            await status.edit_text(
                "❌ UPDATE ERROR\n\n"
                + str(exc)[-3500:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                + "",
                parse_mode=None,
            )

    @app.on_message(filters.command("ai"))
    async def ai_cmd(client: Client, message: Message):
        prompt = message.text.split(None, 1)[1].strip() if len(message.command) > 1 else ""
        if not prompt:
            await message.reply_text("Usage: /ai <your question>", quote=True, parse_mode=None)
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

    @app.on_callback_query(filters.regex(r"^prov:"))
    async def on_provider_chosen(client: Client, callback_query: CallbackQuery):
        _, provider, token = callback_query.data.split(":", 2)
        pending = _pending.get(token)

        if pending is None:
            await callback_query.answer(
                "This request expired, please send /ai again.", show_alert=True
            )
            return

        await callback_query.answer()
        pending["provider"] = provider
        try:
            await callback_query.message.edit_text(
                f"Provider: {PROVIDER_LABELS.get(provider, provider)}\nPick a model:",
                parse_mode=None,
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
                "This request expired, please send /ai again.", show_alert=True
            )
            return

        await callback_query.answer()
        try:
            await callback_query.message.edit_text(
                "Pick a provider:",
                parse_mode=None,
                reply_markup=_provider_keyboard(token),
            )
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^model:"))
    async def on_model_chosen(client: Client, callback_query: CallbackQuery):
        _, model, token = callback_query.data.split(":", 2)
        pending = _pending.pop(token, None)

        if pending is None:
            await callback_query.answer(
                "This request expired, please send /ai again.", show_alert=True
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
