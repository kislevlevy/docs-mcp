"""Secure, body-order-preserving DOCX structural extraction."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..document import ParsedDocument, ResourceLimits, SourceBlock
from ..normalize import normalize_text
from ..quality import MaterializationError

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph(element: ET.Element) -> tuple[str, int | None, str]:
    # Both ordinary Word text (w:t) and Office Math text (m:t) use a local
    # element name of `t`; walking body order keeps inline formula identifiers.
    text = normalize_text(
        "".join(
            node.text or "" for node in element.iter() if node.tag.rsplit("}", 1)[-1] == "t"
        ),
        preserve_layout=True,
    )
    style = element.find(f"./{_W}pPr/{_W}pStyle")
    value = style.get(f"{_W}val", "") if style is not None else ""
    level = None
    if value.lower().startswith("heading"):
        digits = "".join(ch for ch in value if ch.isdigit())
        level = max(1, min(int(digits or "1"), 6))
    if level:
        kind = "heading"
    elif any(node.tag.rsplit("}", 1)[-1] in {"oMath", "oMathPara"} for node in element.iter()):
        kind = "formula"
    elif "code" in value.lower() or "preformatted" in value.lower():
        kind = "code"
    elif element.find(f"./{_W}pPr/{_W}numPr") is not None:
        kind = "list"
        text = f"- {text}"
    else:
        kind = "paragraph"
    return text, level, kind


def _table(element: ET.Element) -> str:
    rows: list[list[str]] = []
    for row in element.findall(f"./{_W}tr"):
        cells = []
        for cell in row.findall(f"./{_W}tc"):
            cells.append(normalize_text(" ".join(node.text or "" for node in cell.iter(f"{_W}t"))))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(map(len, rows))
    rows = [row + [""] * (width - len(row)) for row in rows]
    def escaped(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")
    rendered = ["| " + " | ".join(map(escaped, rows[0])) + " |"]
    rendered.append("| " + " | ".join(["---"] * width) + " |")
    rendered.extend("| " + " | ".join(map(escaped, row)) + " |" for row in rows[1:])
    return "\n".join(rendered)


def parse_docx(path: Path, limits: ResourceLimits) -> ParsedDocument:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_docx_entries:
                raise MaterializationError("too_many_zip_entries", "DOCX contains too many ZIP entries")
            expanded = sum(info.file_size for info in infos)
            if expanded > limits.max_docx_expanded_bytes:
                raise MaterializationError("docx_expansion_too_large", "expanded DOCX is too large")
            for info in infos:
                parts = Path(info.filename).parts
                if Path(info.filename).is_absolute() or ".." in parts:
                    raise MaterializationError("unsafe_zip_path", "DOCX contains an unsafe ZIP path")
                if info.filename.lower().endswith("vbaproject.bin"):
                    raise MaterializationError("docx_macro", "macro-enabled DOCX content is not supported")
            for info in infos:
                if info.filename.lower().endswith(".rels"):
                    relationships = archive.read(info)
                    if b'TargetMode="External"' in relationships or b"TargetMode='External'" in relationships:
                        raise MaterializationError(
                            "external_relationship", "external DOCX relationships are not supported"
                        )
            xml = archive.read("word/document.xml")
    except MaterializationError:
        raise
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise MaterializationError("invalid_docx", "invalid DOCX container") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MaterializationError("invalid_docx_xml", "invalid DOCX document XML") from exc
    body = root.find(f".//{_W}body")
    if body is None:
        raise MaterializationError("invalid_docx_xml", "DOCX has no document body")
    blocks: list[SourceBlock] = []
    for body_index, element in enumerate(body):
        if element.tag == f"{_W}p":
            text, level, kind = _paragraph(element)
        elif element.tag == f"{_W}tbl":
            text, level, kind = _table(element), None, "table"
        else:
            continue
        if text:
            blocks.append(
                SourceBlock(
                    index=body_index,
                    kind=kind,
                    text=text,
                    level=level,
                    page_block_index=0,
                    method="native",
                )
            )
    return ParsedDocument(
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        blocks=tuple(blocks),
        parser_name="docx-ooxml",
        parser_version="1",
    )
