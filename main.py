"""
Entrypoint for the Telegram bot (Kurigram).

Sends every prompt (plain message or /ai) to the single model configured in
AI_MODEL on https://9.todict.tech/v1, streams the reply back with reasoning
shown in an expandable blockquote, and exposes admin commands for the VPS
(`/sh`), broadcasting and GitHub auto-update (`/gitpull`).
"""
from __future__ import annotations

import logging
import os

from bot.client import create_client
from bot.handlers import register_handlers
from bot.updater import sync
from config import settings, validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    validate()

    if settings.git_auto_pull:
        ok, report = sync(settings.git_repo_dir, settings.git_branch)
        logger.info("git auto-pull %s: %s", "ok" if ok else "FAILED", report.replace("\n", " | "))

    app = create_client()
    register_handlers(app)

    logger.info("Bot is running... (Ctrl+C to stop)")
    app.run()

if __name__ == "__main__":
    main()
