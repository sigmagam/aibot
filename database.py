"""
Small SQLite persistence layer for Telegram users.

Stores users who have started the bot so the admin can broadcast updates
to people who explicitly opened the bot with /start.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config import settings


def _db_path() -> str:
    path = settings.database_path
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users(chat_id)
        """)


def upsert_user(message) -> None:
    user = message.from_user
    if not user:
        return
    with connect() as conn:
        conn.execute("""
            INSERT INTO users
                (user_id, chat_id, username, first_name, last_name, is_bot)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                is_bot=excluded.is_bot,
                updated_at=CURRENT_TIMESTAMP
        """, (
            int(user.id),
            int(message.chat.id),
            user.username,
            user.first_name,
            user.last_name,
            int(bool(user.is_bot)),
        ))


def get_broadcast_targets() -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM users WHERE is_bot = 0 ORDER BY chat_id"
        ).fetchall()
    return [int(row["chat_id"]) for row in rows]


def count_users() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM users WHERE is_bot = 0"
        ).fetchone()
    return int(row["total"])


def remove_user(chat_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
