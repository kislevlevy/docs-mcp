"""Versioned manifest validation and unified index-document discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DOC_EXTENSIONS
from .document import DEFAULT_LIMITS, JSONValue, MANIFEST_VERSION, ResourceLimits
from .formats import walk_supported


class ParserIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)


class BlockLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int | None = Field(default=None, ge=1)
    document_index: int | None = Field(default=None, ge=0)
    index: int = Field(ge=0)

    @model_validator(mode="after")
    def has_locator(self):
        if self.page is None and self.document_index is None:
            raise ValueError("block location requires page or document_index")
        return self


class MaterializedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    section_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=10_000)
    heading_path: list[str] = Field(default_factory=list, max_length=100)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    page_labels: list[str] = Field(default_factory=list, max_length=2_000)
    block_start: BlockLocation
    block_end: BlockLocation
    block_count: int | None = Field(default=None, ge=1)
    section_confidence: float = Field(ge=0, le=1)
    content_kinds: list[str] = Field(default_factory=list, max_length=100)
    extraction_methods: list[str] = Field(default_factory=list, max_length=100)
    search_aliases: list[str] = Field(default_factory=list, max_length=1_000)
    warnings: list[str] = Field(default_factory=list, max_length=1_000)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or path.is_absolute()
            or len(path.parts) != 1
            or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
            or path.suffix.lower() != ".md"
        ):
            raise ValueError("generated path must be a safe Markdown filename")
        return value

    @field_validator(
        "heading_path",
        "page_labels",
        "content_kinds",
        "extraction_methods",
        "search_aliases",
        "warnings",
    )
    @classmethod
    def bounded_strings(cls, values: list[str]) -> list[str]:
        if any(len(value) > 10_000 for value in values):
            raise ValueError("manifest list contains an oversized string")
        return values

    @model_validator(mode="after")
    def ordered_ranges(self):
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("page range must have both endpoints or neither")
        if self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("page range is reversed")
        if self.block_start.page is not None:
            start = (self.block_start.page, self.block_start.index)
            end = (self.block_end.page or 0, self.block_end.index)
        else:
            start = (self.block_start.document_index or 0, self.block_start.index)
            end = (self.block_end.document_index or 0, self.block_end.index)
        if end < start:
            raise ValueError("block range is reversed")
        return self


class MaterializedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_version: int = Field(default=MANIFEST_VERSION)
    source: str = Field(min_length=1, max_length=200)
    origin_path: str
    origin_media_type: str = Field(min_length=1, max_length=200)
    origin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int | None = Field(default=None, ge=1)
    parser: ParserIdentity
    pipeline_fingerprint: str = Field(min_length=1, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=10_000)
    ocr_pages: list[int] = Field(default_factory=list, max_length=2_000)
    documents: list[MaterializedDocument] = Field(default_factory=list, max_length=20_000)

    @field_validator("manifest_version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version: {value}")
        return value

    @field_validator("origin_path")
    @classmethod
    def safe_origin(cls, value: str) -> str:
        path = PurePosixPath(value)
        if "\\" in value or path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} or part.startswith(".") for part in path.parts
        ):
            raise ValueError("origin_path must be a safe relative path")
        return value

    @field_validator("warnings")
    @classmethod
    def bounded_warnings(cls, values: list[str]) -> list[str]:
        if any(len(value) > 10_000 for value in values):
            raise ValueError("manifest warning is too long")
        return values

    @model_validator(mode="after")
    def unique_documents(self):
        paths = [document.path for document in self.documents]
        ids = [document.section_id for document in self.documents]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate generated document path")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate section_id")
        for document in self.documents:
            if self.page_count is None and document.page_start is not None:
                raise ValueError("DOCX manifest must not fabricate page ranges")
            if self.page_count is not None and document.page_end is not None and document.page_end > self.page_count:
                raise ValueError("document page range exceeds page_count")
        if self.page_count is None and self.ocr_pages:
            raise ValueError("DOCX manifest must not contain OCR page numbers")
        if self.page_count is not None and any(page > self.page_count for page in self.ocr_pages):
            raise ValueError("OCR page exceeds page_count")
        if self.ocr_pages != sorted(set(self.ocr_pages)):
            raise ValueError("OCR pages must be unique and ordered")
        return self


@dataclass(frozen=True, slots=True)
class IndexDocument:
    source: str
    logical_path: str
    filesystem_path: Path
    title: str | None
    origin_path: str
    origin_media_type: str
    section_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)


def manifest_json(manifest: MaterializedManifest) -> bytes:
    return (json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_manifest(
    directory: Path,
    *,
    expected_source: str | None = None,
    expected_origin: str | None = None,
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> MaterializedManifest:
    path = directory / "_manifest.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"materialized manifest is missing: {path}")
    if path.stat().st_size > limits.max_manifest_bytes:
        raise ValueError("materialized manifest is too large")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        manifest = MaterializedManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid materialized manifest: {path}") from exc
    if expected_source is not None and manifest.source != expected_source:
        raise ValueError("manifest source does not match its directory")
    if expected_origin is not None and manifest.origin_path != expected_origin:
        raise ValueError("manifest origin does not match its directory")
    listed = {document.path for document in manifest.documents}
    actual = {entry.name for entry in directory.iterdir() if entry.is_file() and entry.suffix.lower() == ".md"}
    if listed != actual:
        raise ValueError("manifest and generated Markdown files do not match")
    for document in manifest.documents:
        candidate = directory / document.path
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"generated Markdown is missing: {document.path}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if document.content_sha256 is not None and digest != document.content_sha256:
            raise ValueError(f"generated Markdown hash mismatch: {document.path}")
    return manifest


def native_documents(source: str, root: Path) -> list[IndexDocument]:
    paths, _ = walk_supported(root, "git")
    return [
        IndexDocument(
            source=source,
            logical_path=path.relative_to(root).as_posix(),
            filesystem_path=path,
            title=None,
            origin_path=path.relative_to(root).as_posix(),
            origin_media_type={".md": "text/markdown", ".mdx": "text/mdx", ".rst": "text/x-rst", ".txt": "text/plain"}.get(path.suffix.lower(), "text/plain"),
        )
        for path in paths
        if path.suffix.lower() in DOC_EXTENSIONS
    ]


def materialized_documents(source: str, materialized_source_root: Path) -> list[IndexDocument]:
    if not materialized_source_root.is_dir():
        return []
    documents: list[IndexDocument] = []
    for manifest_path in sorted(materialized_source_root.rglob("_manifest.json")):
        directory = manifest_path.parent
        try:
            directory.resolve().relative_to(materialized_source_root.resolve())
            relative_directory = directory.relative_to(materialized_source_root)
        except ValueError:
            raise ValueError("materialized manifest escapes its source root")
        cursor = materialized_source_root
        for part in relative_directory.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("symlinks are not allowed in materialized output")
        manifest = load_manifest(directory, expected_source=source)
        for entry in manifest.documents:
            logical_path = f"{manifest.origin_path}/{entry.path}"
            documents.append(
                IndexDocument(
                    source=source,
                    logical_path=logical_path,
                    filesystem_path=directory / entry.path,
                    title=entry.title,
                    origin_path=manifest.origin_path,
                    origin_media_type=manifest.origin_media_type,
                    section_id=entry.section_id,
                    page_start=entry.page_start,
                    page_end=entry.page_end,
                    metadata={
                        "page_labels": entry.page_labels,
                        "extraction_methods": entry.extraction_methods,
                        "warnings": entry.warnings,
                        "content_kinds": entry.content_kinds,
                        "search_aliases": entry.search_aliases,
                        "materialization_fingerprint": manifest.pipeline_fingerprint,
                        "heading_path": entry.heading_path,
                    },
                )
            )
    return documents


def discover_index_documents(source: str, root: Path, materialized_root: Path) -> list[IndexDocument]:
    return sorted(
        native_documents(source, root) + materialized_documents(source, materialized_root / source),
        key=lambda document: document.logical_path,
    )
