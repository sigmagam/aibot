"""
Wrapper tipis untuk fitur Rich Messages (Bot API 10.1, rilis 11 Juni 2026):
    - sendRichMessage
    - sendRichMessageDraft
    - block RichBlockThinking -> dipakai buat nampilin proses "thinking"
      model reasoning secara streaming, mirip ChatGPT.

Kurigram (MTProto client) belum tentu membungkus method HTTP-only ini
(fitur ini benar-benar baru), jadi kita panggil langsung ke endpoint HTTP
Bot API pakai bot token yang sama. Kalau panggilan gagal (fitur belum aktif
di klien user, error skema, dsb) kelas ini melempar RichNotSupportedError
supaya pemanggil bisa fallback ke pesan teks biasa lewat Kurigram.
"""
from __future__ import annotations

from typing import Optional

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"


class RichNotSupportedError(Exception):
    """Dilempar kalau sendRichMessage/sendRichMessageDraft gagal / belum didukung."""


def thinking_block(text: str) -> dict:
    """Block untuk menampilkan proses berpikir model (collapsible di klien Telegram)."""
    return {"type": "thinking", "text": text}


def paragraph_block(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def code_block(text: str, language: str = "") -> dict:
    return {"type": "code", "text": text, "language": language}


class RichMessageClient:
    def __init__(self, bot_token: str) -> None:
        self._base = f"{TELEGRAM_API_BASE}/bot{bot_token}"

    async def _post(self, method: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self._base}/{method}", json=payload)
            data = resp.json()
            if not data.get("ok"):
                raise RichNotSupportedError(
                    f"{method} gagal: {data.get('description', resp.text)}"
                )
            return data["result"]

    async def send_rich_message(
        self,
        chat_id: int,
        blocks: list[dict],
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "rich_message": {"blocks": blocks},
        }
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        return await self._post("sendRichMessage", payload)

    async def send_rich_message_draft(
        self,
        chat_id: int,
        draft_id: str,
        blocks: list[dict],
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": {"blocks": blocks},
        }
        return await self._post("sendRichMessageDraft", payload)
