"""
Lightweight Markdown (the style most LLMs output) -> HTML conversion for
Telegram's parse_mode: HTML (<b>, <i>, <code>, <pre>, <blockquote>).

No external library, so it's easy to tweak. Handles:
    **bold** / __bold__          -> <b>...</b>
    *italic* / _italic_          -> <i>...</i>
    `inline code`                -> <code>...</code>
    ```lang\ncode```              -> <pre><code class="language-lang">...</code></pre>
    lines starting with "> "     -> <blockquote>...</blockquote>
    # Heading                    -> <b>Heading</b>
    | a | b | tables             -> <pre> (monospace, keeps the columns lined up)
    --- / ***                    -> dropped (Telegram has no horizontal rule)
"""
from __future__ import annotations

import html
import re

_CODE_BLOCK_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# A markdown table row: has at least two "|" (or starts/ends with one and
# contains another). The separator row (|---|---|) is what confirms it.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_STRIP_INLINE_RE = re.compile(r"</?(?:b|strong|i|em|code|pre|u|s|del|blockquote)\s*/?>")


def md_to_html(text: str) -> str:
    if not text:
        return text

    # 1) Stash fenced code blocks (```...```) first so nothing else touches them.
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = m.group(1)
        code = html.escape(m.group(2).strip("\n"))
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>'
        else:
            tag = f"<pre>{code}</pre>"
        blocks.append(tag)
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_stash_block, text)

    # 2) Stash inline code (`...`).
    inline: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        inline.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00INLINE{len(inline) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 3) Escape everything else so it's safe as HTML.
    text = html.escape(text)

    # 4) Bold & italic.
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

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

    # 6) Headings, tables and horizontal rules — cosmetic tidying so raw
    # markdown never shows up in the chat.
    tidy_lines: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        heading = _HEADING_RE.match(line)
        if heading:
            tidy_lines.append(f"<b>{heading.group(1).strip()}</b>")
            i += 1
            continue

        if _RULE_RE.match(line):
            i += 1
            continue

        # Table: a run of "| ... |" rows (with an optional separator row) is
        # rendered as one monospace block, so the pipes line up in Telegram
        # instead of looking like garbage.
        if _TABLE_ROW_RE.match(line):
            run = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                if not _TABLE_SEP_RE.match(lines[i]):
                    # <pre> can't contain <b>/<i>/<code>: Telegram would
                    # print the tags literally, so strip them here.
                    run.append(_STRIP_INLINE_RE.sub("", lines[i].strip()))
                i += 1
            if run:
                tidy_lines.append("<pre>" + "\n".join(run) + "</pre>")
            continue

        tidy_lines.append(line)
        i += 1
    text = "\n".join(tidy_lines)

    # 7) Put back the stashed code blocks & inline code.
    for i, val in enumerate(inline):
        text = text.replace(f"\x00INLINE{i}\x00", val)
    for i, val in enumerate(blocks):
        text = text.replace(f"\x00BLOCK{i}\x00", val)

    return text
