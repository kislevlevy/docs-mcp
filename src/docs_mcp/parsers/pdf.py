"""Local native-text PDF adapter used by the materialization framework.

This deliberately performs no blanket OCR. Pages without usable native text are
reported, allowing a future selective OCR backend to fill only those pages.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pypdf
from pypdf import PdfReader

from ..document import DocumentWarning, ParsedDocument, ResourceLimits, SourceBlock
from ..normalize import normalize_text
from ..quality import MaterializationError

_NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|chapter\s+\d+|פרק\s+\S+)[ \t]+\S", re.I)


def _usable_native(text: str) -> bool:
    nonspace = [character for character in text if not character.isspace()]
    if len(nonspace) < 8:
        return False
    suspicious = sum(
        character == "\ufffd" or (ord(character) < 32 and character not in "\n\t")
        for character in nonspace
    )
    return suspicious / len(nonspace) < 0.1


def _ocr_page(path: Path, page_number: int, limits: ResourceLimits) -> str | None:
    renderer = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not renderer or not tesseract:
        return None
    with tempfile.TemporaryDirectory(prefix="docs-mcp-ocr-") as temp:
        output = Path(temp) / "page"
        try:
            subprocess.run(
                [
                    renderer,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-r",
                    str(limits.ocr_dpi),
                    "-png",
                    "-singlefile",
                    str(path),
                    str(output),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=limits.max_ocr_seconds_per_page,
            )
            result = subprocess.run(
                [tesseract, str(output.with_suffix(".png")), "stdout", "-l", "heb+eng"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=limits.max_ocr_seconds_per_page,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
    return result.stdout.decode("utf-8", errors="replace")


def _kind(line: str) -> tuple[str, int | None]:
    stripped = line.strip()
    if _NUMBERED_HEADING.match(stripped) or (
        2 <= len(stripped) <= 100
        and stripped == stripped.upper()
        and any(ch.isalpha() for ch in stripped)
    ):
        return "heading", 1
    if stripped.startswith(("- ", "* ", "• ")):
        return "list", None
    return "paragraph", None


def parse_pdf(path: Path, limits: ResourceLimits) -> ParsedDocument:
    started = time.monotonic()
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise MaterializationError("encrypted_pdf", "encrypted PDFs are not supported")
        page_count = len(reader.pages)
        if page_count > limits.max_pdf_pages:
            raise MaterializationError("too_many_pages", f"document has {page_count} pages")
    except MaterializationError:
        raise
    except Exception as exc:
        raise MaterializationError("invalid_pdf", "invalid or unreadable PDF") from exc

    blocks: list[SourceBlock] = []
    warnings: list[DocumentWarning] = []
    index = 0
    for page_number, page in enumerate(reader.pages, 1):
        if time.monotonic() - started > limits.max_processing_seconds:
            raise MaterializationError("processing_timeout", "PDF processing time limit exceeded")
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:
            warnings.append(DocumentWarning("page_extract_failed", f"page {page_number}: {exc}"))
            continue
        normalized = normalize_text(extracted, preserve_layout=True)
        method = "native"
        if not _usable_native(normalized):
            width = float(page.mediabox.width) / 72 * limits.ocr_dpi
            height = float(page.mediabox.height) / 72 * limits.ocr_dpi
            if width * height > limits.max_rendered_pixels:
                raise MaterializationError(
                    "rendered_page_too_large",
                    f"page {page_number} would render to too many pixels",
                )
            ocr_text = _ocr_page(path, page_number, limits)
            if ocr_text:
                normalized = normalize_text(ocr_text, preserve_layout=True)
                method = "ocr"
            else:
                code = "ocr_required" if shutil.which("tesseract") is None else "ocr_failed"
                warnings.append(DocumentWarning(code, f"page {page_number} has no usable native text"))
                continue
        page_block = 0
        # Blank-line groups retain paragraphs while avoiding one block per wrapped line.
        for group in re.split(r"\n\s*\n", normalized):
            text = normalize_text(group, preserve_layout=True)
            if not text:
                continue
            lines = text.splitlines()
            kind, level = _kind(lines[0])
            pieces = (
                [(kind, level, lines[0]), ("paragraph", None, "\n".join(lines[1:]))]
                if kind == "heading" and len(lines) > 1
                else [(kind, level, text)]
            )
            for piece_kind, piece_level, piece_text in pieces:
                piece_text = normalize_text(piece_text, preserve_layout=True)
                if not piece_text:
                    continue
                blocks.append(
                    SourceBlock(
                        index=index,
                        kind=piece_kind,
                        text=piece_text,
                        level=piece_level,
                        page=page_number,
                        page_block_index=page_block,
                        method=method,
                    )
                )
                index += 1
                page_block += 1
    return ParsedDocument(
        media_type="application/pdf",
        blocks=tuple(blocks),
        parser_name="pypdf-native",
        parser_version=getattr(pypdf, "__version__", "unknown"),
        page_count=page_count,
        warnings=tuple(warnings),
    )
