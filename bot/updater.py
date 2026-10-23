"""
Auto-update from GitHub.

Used by `main.py` at startup (when GIT_AUTO_PULL is on) and by the
`/gitpull` command. Pull-only: `git pull --ff-only` + `pip install -r
requirements.txt`. This never commits or pushes anything.

Never raises — a failed update must not take the bot down, the caller just
shows/report the output.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger("bot.updater")


def sync(repo_dir: str, branch: str, install_requirements: bool = True) -> tuple[bool, str]:
    """Update the deployed checkout. Returns (ok, report)."""
    repo = os.path.abspath(repo_dir)
    try:
        pull = subprocess.run(
            ["git", "-C", repo, "pull", "--ff-only", "origin", branch],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - never break the bot over an update
        return False, f"git pull failed: {exc}"

    if pull.returncode != 0:
        return False, (pull.stderr or pull.stdout or "git pull failed").strip()

    requirements = os.path.join(repo, "requirements.txt")
    if install_requirements and os.path.exists(requirements):
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", requirements, "-q"],
            capture_output=True, text=True, timeout=180,
        )
        if pip.returncode != 0:
            return False, "Code updated, dependency install failed:\n" + (
                pip.stderr or pip.stdout or ""
            ).strip()

    commit = ""
    try:
        commit = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        pass

    return True, f"commit {commit or 'unknown'}"
