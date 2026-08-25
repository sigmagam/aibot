"""
Entrypoint for the Telegram AI bot (Kurigram). Routes prompts to
https://9.todict.tech/v1 with model fallback, and lets the user pick a
🧠 Thinking or ⚡ Direct reply mode via an inline button — works in
private chats, via @botname mention / reply-to-bot in groups, or the
explicit /ai <prompt> command anywhere.
"""
from __future__ import annotations

import logging
import os

from bot.client import create_client
from bot.handlers import register_handlers
from config import validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    validate()
    app = create_client()
    register_handlers(app)

    logger.info("Bot is running... (Ctrl+C to stop)")
    app.run()


if __name__ == "__main__":
    main()
