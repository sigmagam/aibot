"""
Lightweight Markdown (the style most LLMs output) -> HTML conversion for
Telegram's parse_mode: , , , , <blockquote>.

No external library, so it's easy to tweak. Handles:
    **bold** / __bold__          -> ...
    *italic* / _italic_          -> ...
    `inline code`                -> ...
    ```lang\ncode```              -> <code class="language-lang">...
    lines starting with "> "     -> <blockquote>...</blockquote>
"""
from __future__ import annotations

import html
import re

_CODE_BLOCK_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")


def md_to_html(text: str) -> str:
    if not text:
        return text

    # 1) Stash fenced code blocks (```...```) first so nothing else touches them.
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = m.group(1)
        code = html.escape(m.group(2).strip("\n"))
        tag = f'<code class="language-{lang}">{code}' if lang else f"{code}"
        blocks.append(tag)
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_stash_block, text)

    # 2) Stash inline code (`...`).
    inline: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        inline.append(f"{html.escape(m.group(1))}")
        return f"\x00INLINE{len(inline) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 3) Escape everything else so it's safe as HTML.
    text = html.escape(text)

    # 4) Bold & italic.
    text = _BOLD_RE.sub(lambda m: f"{m.group(1) or m.group(2)}", text)
    text = _ITALIC_RE.sub(lambda m: f"{m.group(1) or m.group(2)}", text)

    # 5) Blockquote: group consecutive lines starting with "> " (already
    # escaped to "&gt; " by this point).
    out_lines: list[str] = []
    quote_buf: list[str] = []

    def _flush_quote() -> None:
        if quote_buf:
            out_lines.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in text.split("\n"):
        if line.startswith("&gt; "):
            quote_buf.append(line[len("&gt; "):])
        elif line.startswith("&gt;"):
            quote_buf.append(line[len("&gt;"):])
        else:
            _flush_quote()
            out_lines.append(line)
    _flush_quote()
    text = "\n".join(out_lines)

    # 6) Put back the stashed code blocks & inline code.
    for i, val in enumerate(inline):
        text = text.replace(f"\x00INLINE{i}\x00", val)
    for i, val in enumerate(blocks):
        text = text.replace(f"\x00BLOCK{i}\x00", val)

    return text
