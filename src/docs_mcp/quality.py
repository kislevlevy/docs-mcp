"""Central resource and output quality checks."""

from __future__ import annotations

from pathlib import Path

from .document import DEFAULT_LIMITS, ParsedDocument, ResourceLimits


class MaterializationError(ValueError):
    """A stable fatal error which must not replace last-known-good output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_input(path: Path, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
    size = path.stat().st_size
    if size > limits.max_input_bytes:
        raise MaterializationError("input_too_large", f"input is {size} bytes")


def check_parsed(doc: ParsedDocument, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
    if doc.page_count is not None and doc.page_count > limits.max_pdf_pages:
        raise MaterializationError(
            "too_many_pages", f"document has {doc.page_count} pages"
        )
    characters = sum(len(block.text) for block in doc.blocks)
    if characters > limits.max_extracted_characters:
        raise MaterializationError(
            "too_many_characters", f"document has {characters} extracted characters"
        )
    if not any(block.text.strip() for block in doc.blocks):
        raise MaterializationError("no_extractable_text", "document contains no extractable text")
