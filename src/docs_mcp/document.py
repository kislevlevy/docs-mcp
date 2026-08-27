"""Normalized rich-document values shared by parsers and materialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

MANIFEST_VERSION = 1
MATERIALIZATION_FORMAT_VERSION = 1
NORMALIZATION_VERSION = 1
SEGMENTATION_VERSION = 1
MARKDOWN_RENDERER_VERSION = 1


@dataclass(frozen=True, slots=True)
class DocumentWarning:
    code: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """One ordered, normalized unit from a PDF page or DOCX body."""

    index: int
    kind: str
    text: str
    level: int | None = None
    page: int | None = None
    page_label: str | None = None
    page_block_index: int = 0
    method: str = "native"
    language: str | None = None
    warnings: tuple[DocumentWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    media_type: str
    blocks: tuple[SourceBlock, ...]
    parser_name: str
    parser_version: str
    page_count: int | None = None
    warnings: tuple[DocumentWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class Topic:
    section_id: str
    title: str
    heading_path: tuple[str, ...]
    blocks: tuple[SourceBlock, ...]
    confidence: float
    warnings: tuple[DocumentWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_input_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 2_000
    max_extracted_characters: int = 20_000_000
    max_docx_entries: int = 10_000
    max_docx_expanded_bytes: int = 500 * 1024 * 1024
    max_manifest_documents: int = 20_000
    max_manifest_bytes: int = 20 * 1024 * 1024
    max_string_length: int = 1_000_000
    max_list_items: int = 20_000
    max_ocr_seconds_per_page: int = 120
    ocr_dpi: int = 200
    max_rendered_pixels: int = 50_000_000
    max_processing_seconds: int = 1_800


DEFAULT_LIMITS = ResourceLimits()


@dataclass(slots=True)
class MaterializationStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    topics: int = 0
    pages: int = 0
    blocks: int = 0
    ocr_pages: int = 0
    warnings: int = 0
    errors: list[str] = field(default_factory=list)
    parser_identities: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def merge(self, other: "MaterializationStats") -> None:
        for name in (
            "added",
            "changed",
            "removed",
            "unchanged",
            "failed",
            "topics",
            "pages",
            "blocks",
            "ocr_pages",
            "warnings",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.errors.extend(other.errors)
        self.parser_identities = sorted(
            set(self.parser_identities) | set(other.parser_identities)
        )
        self.elapsed_seconds += other.elapsed_seconds
