"""
Maps the plain emoji already used across bot messages to real Telegram
custom emoji IDs, and wraps them in <emoji id="..."> tags (Kurigram/Pyrogram
also accepts <tg-emoji emoji-id="...">, same thing) so they render as the
custom animated/branded versions instead of the default Unicode glyph.

Off by default via ENABLE_CUSTOM_EMOJI, since custom emoji entities only
render for bots that bought an extra username on Fragment, or when the
message is sent directly to a chat by a bot owned by a Telegram Premium
account — turn it on once you've confirmed your bot meets one of those.
"""
from __future__ import annotations

import os
import re

# char -> custom emoji document id (as given by the user).
EMOJI_MAP: dict[str, str] = {
    "⚠️": "5447644880824181073",
    "⚠": "5447644880824181073",
    "🧭": "5433825729060018456",
    "📣": "4958686613933655185",
    "⚡️": "5456140674028019486",
    "⚡": "5456140674028019486",
    "🧩": "6030802547998986847",
    "👥️": "5316740582654112585",
    "👥": "5316740582654112585",
    "✅": "6026257381678124710",
    "💡": "5123359615727174427",
    "🚀": "5445284980978621387",
    "📦": "5298900838989701082",
    "📚": "5373098009640836781",
    "🎛️": "5159032317007627730",
    "🎛": "5159032317007627730",
    "📊": "4958506272551863292",
    "🗃️": "5346288231073723227",
    "🗃": "5346288231073723227",
    "🔄": "6032964711845204323",
    "📌": "5397782960512444700",
    "♻️": "5377584064326804458",
    "♻": "5377584064326804458",
    "❌": "5161208387957950108",
    "✖️": "5161208387957950108",
    "✖": "5161208387957950108",
    # from the earlier button-icon batch, reused for in-text occurrences too
    "🧠": "5237799019329105246",
    "✨": "5472164874886846699",
    "⬅️": "5258236805890710909",
    "⬅": "5258236805890710909",
    "🤖": "5372981976804366741",
    "🧹": "4956591954088428445",
}

ENABLE_CUSTOM_EMOJI = os.getenv("ENABLE_CUSTOM_EMOJI", "false").lower() == "true"

# Longest keys first so a variation-selector variant (e.g. "⚡️") is matched
# and replaced before its bare base character (e.g. "⚡").
_ORDERED_CHARS = sorted(EMOJI_MAP, key=len, reverse=True)
_EMOJI_RE = re.compile("|".join(re.escape(c) for c in _ORDERED_CHARS)) if _ORDERED_CHARS else None


def wrap_custom_emoji(html_text: str) -> str:
    """Replace known plain emoji in an HTML-parse-mode message with
    <emoji id="..."> tags. No-op when ENABLE_CUSTOM_EMOJI is off, the text
    is empty, or the map is empty."""
    if not ENABLE_CUSTOM_EMOJI or not html_text or _EMOJI_RE is None:
        return html_text
    return _EMOJI_RE.sub(lambda m: f'<emoji id="{EMOJI_MAP[m.group(0)]}">{m.group(0)}</emoji>', html_text)
