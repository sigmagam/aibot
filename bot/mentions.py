"""
Helpers to decide whether an incoming message should trigger the AI, and
to build the prompt text out of it.

Every text message triggers a response, in private chats and in groups /
supergroups alike (mention / reply-to-bot still gets stripped into a clean
prompt if present, but is no longer required in groups). Note that in
groups this only works if the bot actually *receives* the message: unless
the bot is a group admin, Telegram's Bot API "Privacy Mode" (set via
@BotFather -> /setprivacy) hides plain messages from the bot entirely, so
Privacy Mode must be disabled for this to work — see README.md.
"""
from __future__ import annotations

from typing import Optional

from pyrogram.enums import MessageEntityType
from pyrogram.types import Message


def _mentions_bot(message: Message, bot_username: str) -> bool:
    text = message.text or message.caption or ""
    if not text or not bot_username:
        return False

    mention_tag = f"@{bot_username}".lower()
    if text.lower().startswith(mention_tag):
        return True

    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == MessageEntityType.MENTION:
            piece = text[entity.offset : entity.offset + entity.length]
            if piece.lower() == mention_tag:
                return True
    return False


def should_respond(message: Message, bot_username: str) -> bool:
    """
    True if this message should be treated as an AI prompt at all.

    Always True for a plain text/caption message — private chat or group,
    mentioned or not. This only decides *whether we treat it as a prompt*;
    whether the bot ever sees the message in the first place (in a group,
    without a mention) is governed by Telegram's Privacy Mode setting on
    the bot, not by this function.
    """
    return True


def extract_prompt(message: Message, bot_username: str) -> Optional[str]:
    """
    Return the prompt text for this message, or None if there's no text
    to work with at all. Strips a leading "@botname" mention so it doesn't
    leak into the prompt sent to the AI.
    """
    text = message.text or message.caption or ""
    if not text:
        return None

    mention_tag = f"@{bot_username}".lower()
    stripped = None

    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == MessageEntityType.MENTION:
            piece = text[entity.offset : entity.offset + entity.length]
            if piece.lower() == mention_tag:
                stripped = (text[: entity.offset] + text[entity.offset + entity.length :]).strip()
                break

    if stripped is None and text.lower().startswith(mention_tag):
        stripped = text[len(mention_tag):].strip()

    if stripped is not None:
        # Bare "@botname" with nothing else -> fall back to the raw text
        # instead of silently dropping the message.
        return stripped or text.strip()

    return text.strip()
