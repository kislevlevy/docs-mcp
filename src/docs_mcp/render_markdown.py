"""Render normalized topic blocks into deterministic inspectable Markdown."""

from __future__ import annotations

from .document import SourceBlock, Topic
from .normalize import normalize_text


def page_marker(page: int, label: str | None = None) -> str:
    suffix = f"; label: {label}" if label else ""
    return f"<!-- docs-mcp-source-page: {page}{suffix} -->"


def _render_block(block: SourceBlock) -> str:
    text = normalize_text(block.text, preserve_layout=block.kind in {"code", "table", "formula"})
    if block.kind == "code":
        return f"```\n{text}\n```"
    if block.kind == "formula":
        return f"$$\n{text}\n$$"
    if block.kind == "heading":
        return f"{'#' * max(1, min(block.level or 1, 6))} {text}"
    return text


def render_topic(topic: Topic) -> str:
    out = [f"# {normalize_text(topic.title)}"]
    current_page: int | None = None
    for position, block in enumerate(topic.blocks):
        if block.page is not None and block.page != current_page:
            out.append(page_marker(block.page, block.page_label))
            current_page = block.page
        # The generated H1 already represents the topic's leading source heading.
        if position == 0 and block.kind == "heading":
            continue
        rendered = _render_block(block)
        if rendered:
            out.append(rendered)
    return "\n\n".join(out).rstrip() + "\n"
