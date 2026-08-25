"""
Global bot configuration. All values come from environment variables
(optionally via a .env file — see .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


@dataclass(frozen=True)
class Settings:
    # --- Telegram credentials (MTProto, required by Kurigram even for a bot) ---
    # Get these at https://my.telegram.org/apps
    api_id: int = _get_int("TG_API_ID", 0)
    api_hash: str = os.getenv("TG_API_HASH", "")
    bot_token: str = os.getenv("TG_BOT_TOKEN", "")

    # --- AI API (router / proxy) ---
    ai_base_url: str = os.getenv("AI_BASE_URL", "https://9.todict.tech/v1")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_timeout: float = _get_float("AI_TIMEOUT", 120.0)

    # --- Bot behavior ---
    max_history: int = _get_int("MAX_HISTORY", 12)  # messages remembered per chat
    session_name: str = os.getenv("TG_SESSION_NAME", "ai_bot")
    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "You are a friendly, concise AI assistant. Always reply in the same "
        "language the user writes in (default to Indonesian if unclear).",
    )

    # --- Streaming reply throttling ---
    # Minimum seconds between message edits while a response is streaming in.
    stream_update_interval: float = _get_float("DRAFT_UPDATE_INTERVAL", 0.8)

    # --- Pending mode-selection buttons ---
    # How long (seconds) a "Thinking / Direct" button stays valid before it
    # expires and the user has to resend the prompt.
    pending_ttl: float = _get_float("PENDING_TTL", 600.0)


settings = Settings()


def validate() -> None:
    missing = []
    if not settings.api_id:
        missing.append("TG_API_ID")
    if not settings.api_hash:
        missing.append("TG_API_HASH")
    if not settings.bot_token:
        missing.append("TG_BOT_TOKEN")
    if not settings.ai_api_key:
        missing.append("AI_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing) +
            "\nCopy .env.example to .env and fill in the values."
        )
