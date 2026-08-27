"""Signature-verified rich-document parser dispatch."""

from __future__ import annotations

from pathlib import Path

from ..document import DEFAULT_LIMITS, ParsedDocument, ResourceLimits
from ..quality import MaterializationError, check_input, check_parsed


def parse_document(path: Path, limits: ResourceLimits = DEFAULT_LIMITS) -> ParsedDocument:
    check_input(path, limits)
    suffix = path.suffix.lower()
    with path.open("rb") as handle:
        signature = handle.read(5)
    if suffix == ".pdf":
        if signature != b"%PDF-":
            raise MaterializationError("invalid_signature", "invalid PDF signature")
        from .pdf import parse_pdf

        result = parse_pdf(path, limits)
    elif suffix == ".docx":
        if signature[:4] != b"PK\x03\x04":
            raise MaterializationError("invalid_signature", "invalid DOCX signature")
        from .docx import parse_docx

        result = parse_docx(path, limits)
    else:
        raise MaterializationError("unsupported_media_type", f"unsupported rich document: {suffix}")
    check_parsed(result, limits)
    return result


__all__ = ["parse_document"]
