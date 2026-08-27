"""Deterministic topic segmentation from explicit document structure."""

from __future__ import annotations

from .document import DocumentWarning, ParsedDocument, SourceBlock, Topic
from .normalize import normalize_text


def _locator(block: SourceBlock) -> str:
    if block.page is not None:
        return f"p{block.page:04d}-b{block.page_block_index:04d}"
    return f"d{block.index + 1:06d}-b{block.page_block_index:04d}"


def _untitled(blocks: list[SourceBlock], confidence: float = 0.25) -> Topic:
    first = blocks[0]
    return Topic(
        section_id=_locator(first),
        title="Untitled section",
        heading_path=(),
        blocks=tuple(blocks),
        confidence=confidence,
        warnings=(
            DocumentWarning(
                "section_boundary_uncertain", "no reliable source heading was available"
            ),
        ),
    )


def _fallback_topics(document: ParsedDocument, page_window: int) -> list[Topic]:
    if document.page_count is None:
        return [_untitled(list(document.blocks))]
    grouped: dict[int, list[SourceBlock]] = {}
    for block in document.blocks:
        grouped.setdefault(block.page or 1, []).append(block)
    topics: list[Topic] = []
    pages = sorted(grouped)
    for start in range(0, len(pages), page_window):
        window = pages[start : start + page_window]
        blocks = [block for page in window for block in grouped[page]]
        topic = _untitled(blocks)
        first, last = window[0], window[-1]
        topics.append(
            Topic(
                section_id=topic.section_id,
                title=f"Untitled section (pages {first}–{last})",
                heading_path=(),
                blocks=topic.blocks,
                confidence=topic.confidence,
                warnings=topic.warnings,
            )
        )
    return topics


def segment_topics(document: ParsedDocument, *, page_window: int = 10) -> tuple[Topic, ...]:
    blocks = list(document.blocks)
    if not blocks:
        return ()
    heading_positions = [i for i, block in enumerate(blocks) if block.kind == "heading"]
    if not heading_positions:
        return tuple(_fallback_topics(document, page_window))

    topics: list[Topic] = []
    heading_stack: list[tuple[int, str]] = []

    # Keep a source preamble inspectable rather than silently attaching a made-up title.
    if heading_positions[0] > 0:
        topics.append(_untitled(blocks[: heading_positions[0]], confidence=0.5))

    for ordinal, start in enumerate(heading_positions):
        end = heading_positions[ordinal + 1] if ordinal + 1 < len(heading_positions) else len(blocks)
        heading = blocks[start]
        level = heading.level or 1
        title = normalize_text(heading.text.splitlines()[0])
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        topics.append(
            Topic(
                section_id=_locator(heading),
                title=title,
                heading_path=tuple(value for _, value in heading_stack),
                blocks=tuple(blocks[start:end]),
                confidence=0.9,
            )
        )
    return tuple(topics)
