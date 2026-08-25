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
from bot.mentions import extract_prompt
from config import settings
from telegram_rich.stream import StreamingReplier

logger = logging.getLogger("bot.handlers")

_COMMANDS = ["start", "help", "reset", "model", "ai"]

# Whether the installed Kurigram/Pyrogram version accepts the "style"
# parameter on InlineKeyboardButton (Bot API 9.4, Feb 2026: "danger" /
# "success" / "primary" button colors). Checked once at import time so
# we never crash on an older library version that doesn't have it yet —
# styling is applied automatically as soon as the library catches up.
_SUPPORTS_BUTTON_STYLE = "style" in inspect.signature(InlineKeyboardButton).parameters


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
            row.append(_button(short_name, f"model:{model}:{token}", style="primary"))
        rows.append(row)
    rows.append([_button("⬅️ Back to providers", f"back:{token}")])
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
    @app.on_message(filters.command("start"))
    async def start_cmd(client: Client, message: Message):
        await message.reply_text(
            "Hi! I'm an AI bot.\n\n"
            "Just type anything — private chat or group, no need to mention "
            "me or use a command. I'll try claude-opus-4-6-thinking first, "
            "then grok-4.5-high, then a random fallback if both fail.\n\n"
            "Want to pick the provider and model yourself instead? Use "
            "/ai <your question> — you'll get buttons to choose from.\n\n"
            "Other commands: /reset (clear chat history), /model (list AI models).",
            parse_mode=None,
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
        lines = ["Default order for plain messages (no /ai):"]
        for i, m in enumerate(MODEL_PRIORITY, start=1):
            lines.append(f"{i}. {m}")
        lines.append(
            "\nIf both fail, the bot automatically tries random models from "
            "every available provider."
        )
        lines.append(
            "\nUse /ai <question> instead to pick the provider and model "
            "yourself via buttons."
        )
        await message.reply_text("\n".join(lines), parse_mode=None)

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
