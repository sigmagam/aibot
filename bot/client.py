"""Build a Kurigram Client instance from configuration."""
from __future__ import annotations

from pyrogram import Client

from config import settings


def create_client() -> Client:
    return Client(
        name=settings.session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,  # no need to persist a session file for a bot token
    )
