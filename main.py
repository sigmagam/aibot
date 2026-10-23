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
import subprocess
import sys

from bot.client import create_client
from bot.handlers import register_handlers
from config import settings, validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


def _auto_pull() -> None:
    """Pull the latest code from GitHub on startup (GIT_AUTO_PULL), then
    reinstall requirements. Pull only — never commits or pushes. Any
    failure is logged and ignored so a broken update can't stop the bot."""
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        pull = subprocess.run(
            ["git", "-C", repo, "pull", "--ff-only", "origin", settings.git_branch],
            capture_output=True, text=True, timeout=120,
        )
        if pull.returncode != 0:
            logger.warning(
                "git auto-pull failed: %s",
                (pull.stderr or pull.stdout or "").strip()[:300],
            )
            return
        logger.info("git auto-pull ok: %s", pull.stdout.strip()[:300])

        requirements = os.path.join(repo, "requirements.txt")
        if os.path.exists(requirements):
            pip = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", requirements, "-q"],
                capture_output=True, text=True, timeout=180,
            )
            if pip.returncode != 0:
                logger.warning(
                    "dependency install failed: %s",
                    (pip.stderr or pip.stdout or "").strip()[:300],
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("git auto-pull skipped: %s", exc)


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    validate()
    if settings.git_auto_pull:
        _auto_pull()
    app = create_client()
    register_handlers(app)

    logger.info("Bot is running... (Ctrl+C to stop)")
    app.run()


if __name__ == "__main__":
    main()
