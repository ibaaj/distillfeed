from __future__ import annotations

import html
import re
from typing import Any

from markupsafe import Markup


_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_UNORDERED = re.compile(r"^\s*[-*+]\s+(.+)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")
# Models occasionally compress several arXiv bullets onto one physical line.
# Split only at a marker followed by an arXiv-style identifier so prose such as
# "human - machine" is not rewritten into a list accidentally.
_INLINE_ARXIV_BULLET = re.compile(
    r"(?<=\S)[ \t]+-[ \t]+(?=(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?[ \t]+(?:—|-))",
    re.IGNORECASE,
)


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


def _normalise_model_markdown(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return _INLINE_ARXIV_BULLET.sub("\n- ", text)


def render_summary_markdown(value: Any) -> Markup:
    """Turn model prose into safe, readable HTML without accepting raw HTML.

    The parser intentionally supports only headings, paragraphs, emphasis,
    inline code, block quotes, and simple lists. Raw HTML is always escaped.
    A plain line immediately preceding a list is treated as a section heading;
    this recovers useful structure from model JSON that omitted ``##`` while
    preserving ordinary prose paragraphs.
    """
    text = _normalise_model_markdown(value)
    if not text:
        return Markup("")

    lines = text.split("\n")
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

    def next_nonempty(index: int) -> str:
        for candidate in lines[index + 1:]:
            stripped = candidate.strip()
            if stripped:
                return stripped
        return ""

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            close_paragraph()
            close_list()
            continue
        heading = _HEADING.match(line)
        unordered = _UNORDERED.match(line)
        ordered = _ORDERED.match(line)
        following = next_nonempty(index)
        inferred_heading = (
            not heading and not unordered and not ordered and not line.startswith("> ")
            and index + 1 < len(lines) and not lines[index + 1].strip()
            and bool(_UNORDERED.match(following) or _ORDERED.match(following))
            and len(line) <= 240
        )
        if heading:
            close_paragraph()
            close_list()
            level = len(heading.group(1)) + 2
            output.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
        elif inferred_heading:
            close_paragraph()
            close_list()
            output.append(f"<h3>{_inline_markdown(line)}</h3>")
        elif unordered or ordered:
            close_paragraph()
            requested_kind = "ul" if unordered else "ol"
            if list_kind != requested_kind:
                close_list()
                list_kind = requested_kind
                output.append(f"<{list_kind}>")
            match = unordered or ordered
            assert match is not None
            output.append(f"<li>{_inline_markdown(match.group(1))}</li>")
        elif line.startswith("> "):
            close_paragraph()
            close_list()
            output.append(f"<blockquote>{_inline_markdown(line[2:])}</blockquote>")
        else:
            close_list()
            paragraph.append(line)
    close_paragraph()
    close_list()
    return Markup("\n".join(output))


def render_plain_text(value: Any) -> Markup:
    """Render source-provided prose as escaped paragraphs without Markdown."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return Markup("")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    rendered = []
    for paragraph in paragraphs:
        lines = "<br>".join(html.escape(line.strip(), quote=True) for line in paragraph.split("\n"))
        rendered.append(f"<p>{lines}</p>")
    return Markup("\n".join(rendered))
