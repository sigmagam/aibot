"""
Single-model catalog.

This bot deliberately runs ONE model — set via `AI_MODEL` in .env, default
`tokenharbor/deepseek-v4.1-flash:free`. There is no provider/model picker:
every prompt (plain message or /ai) goes straight to `MODEL`.
"""
from __future__ import annotations

from config import settings

MODEL: str = settings.ai_model
