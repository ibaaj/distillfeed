from __future__ import annotations

import html
import re
from typing import Any

from markupsafe import Markup


_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_UNORDERED = re.compile(r"^\s*[-*+]\s+(.+)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def _inline_markdown(value: str) -> str:
    """Render a deliberately small, escaped subset of Markdown."""
    escaped = html.escape(value, quote=True)
    code_fragments: list[str] = []

    def preserve_code(match: re.Match[str]) -> str:
        code_fragments.append(f"<code>{match.group(1)}</code>")
        return f"\x00CODE{len(code_fragments) - 1}\x00"

    escaped = re.sub(r"`([^`\n]+)`", preserve_code, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", escaped)
    for index, fragment in enumerate(code_fragments):
        escaped = escaped.replace(f"\x00CODE{index}\x00", fragment)
    return escaped


def render_summary_markdown(value: Any) -> Markup:
    """Turn model prose into safe, readable HTML without accepting raw HTML."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return Markup("")

    output: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None

    def close_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            output.append(f"</{list_kind}>")
            list_kind = None

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            close_paragraph(); close_list(); continue
        heading = _HEADING.match(line)
        unordered = _UNORDERED.match(line)
        ordered = _ORDERED.match(line)
        if heading:
            close_paragraph(); close_list()
            level = len(heading.group(1)) + 2
            output.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
        elif unordered or ordered:
            close_paragraph()
            requested_kind = "ul" if unordered else "ol"
            if list_kind != requested_kind:
                close_list(); list_kind = requested_kind; output.append(f"<{list_kind}>")
            match = unordered or ordered
            assert match is not None
            output.append(f"<li>{_inline_markdown(match.group(1))}</li>")
        elif line.startswith("> "):
            close_paragraph(); close_list()
            output.append(f"<blockquote>{_inline_markdown(line[2:])}</blockquote>")
        else:
            close_list(); paragraph.append(line)
    close_paragraph(); close_list()
    return Markup("\n".join(output))
