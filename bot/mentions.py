"""
Helper to build the prompt text out of an incoming message.

Every text message is treated as a prompt now, in both private chats and
groups — no mention or reply-to-bot required. If the message happens to
start with "@botname ...", that mention is stripped off so it doesn't
leak into the prompt sent to the AI.
"""
from __future__ import annotations

from typing import Optional

from pyrogram.enums import MessageEntityType
from pyrogram.types import Message


def extract_prompt(message: Message, bot_username: str) -> Optional[str]:
    """
    Return the prompt text for this message, or None if there's no text
    to work with at all.
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
