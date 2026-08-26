"""
Helpers to decide whether an incoming message should trigger the AI, and
to build the prompt text out of it.

In private chats every text message is treated as a prompt. In groups /
supergroups, only messages that @mention the bot or are a reply to one of
the bot's own messages trigger a response — otherwise the bot would answer
every single message sent by anyone in every group it's a member of.
"""
from __future__ import annotations

from typing import Optional

from pyrogram.enums import ChatType, MessageEntityType
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
    """True if this message should be treated as an AI prompt at all."""
    if message.chat and message.chat.type == ChatType.PRIVATE:
        return True

    if _mentions_bot(message, bot_username):
        return True

    reply = message.reply_to_message
    if reply and reply.from_user and getattr(reply.from_user, "is_self", False):
        return True

    return False


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
