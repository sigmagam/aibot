"""Build a Kurigram Client instance from configuration."""
from __future__ import annotations

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from config import settings
from telegram_rich.custom_emoji import wrap_custom_emoji

# Patch reply_text/edit_text/send_message once so every HTML-parse-mode
# message anywhere in the bot (handlers.py, telegram_rich/stream.py, ...)
# automatically gets known emoji swapped for their custom-emoji versions.
# Only touches calls that explicitly pass parse_mode=HTML, so plain-text
# error/cancel messages (parse_mode=None) are left untouched.
_orig_reply_text = Message.reply_text
_orig_edit_text = Message.edit_text
_orig_send_message = Client.send_message


async def _patched_reply_text(self, text, *args, parse_mode=None, **kwargs):
    if parse_mode == ParseMode.HTML:
        text = wrap_custom_emoji(text)
    # Telegram Business messages must be answered through the same
    # business connection that delivered the message. Kurigram exposes
    # this ID on Message.business_connection_id.
    business_connection_id = getattr(self, "business_connection_id", None)
    if business_connection_id and "business_connection_id" not in kwargs:
        kwargs["business_connection_id"] = business_connection_id
    return await _orig_reply_text(self, text, *args, parse_mode=parse_mode, **kwargs)


async def _patched_edit_text(self, text, *args, parse_mode=None, **kwargs):
    if parse_mode == ParseMode.HTML:
        text = wrap_custom_emoji(text)
    business_connection_id = getattr(self, "business_connection_id", None)
    if business_connection_id and "business_connection_id" not in kwargs:
        kwargs["business_connection_id"] = business_connection_id
    return await _orig_edit_text(self, text, *args, parse_mode=parse_mode, **kwargs)


async def _patched_send_message(self, chat_id, text, *args, parse_mode=None, **kwargs):
    if parse_mode == ParseMode.HTML:
        text = wrap_custom_emoji(text)
    return await _orig_send_message(self, chat_id, text, *args, parse_mode=parse_mode, **kwargs)


Message.reply_text = _patched_reply_text
Message.edit_text = _patched_edit_text
Client.send_message = _patched_send_message


def create_client() -> Client:
    return Client(
        name=settings.session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,  # no need to persist a session file for a bot token
    )
