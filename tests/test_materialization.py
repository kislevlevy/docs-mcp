"""Rich-document materialization contracts and lifecycle regression tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from docs_mcp import store
from docs_mcp.chunk import split_materialized_document
from docs_mcp.indexer import index_snapshot
from docs_mcp.materialize import inspect_materialization, materialize_source
from docs_mcp.materialized import load_manifest
from docs_mcp.parsers.docx import parse_docx
from docs_mcp.sources import SourceSpec
from docs_mcp.document import DEFAULT_LIMITS


def _docx(path: Path, items: list[tuple[str, str]]) -> None:
    body = []
    for kind, value in items:
        if kind == "heading":
            body.append(
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                f"<w:r><w:t>{value}</w:t></w:r></w:p>"
            )
        elif kind == "table":
            cells = "".join(
                f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>"
                for cell in value.split("|")
            )
            body.append(f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>")
        else:
            body.append(f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_docx_parser_preserves_paragraph_table_order_without_pages(tmp_path):
    path = tmp_path / "manual.docx"
    _docx(path, [("paragraph", "Before"), ("table", "A|B"), ("paragraph", "After")])
    parsed = parse_docx(path, DEFAULT_LIMITS)
    assert [block.kind for block in parsed.blocks] == ["paragraph", "table", "paragraph"]
    assert parsed.blocks[1].text.startswith("| A | B |")
    assert parsed.page_count is None
    assert all(block.page is None for block in parsed.blocks)


def test_materialization_is_deterministic_incremental_and_removes_stale_topics(tmp_path, monkeypatch):
    source = tmp_path / "source"
    state = tmp_path / "state" / "materialized"
    source.mkdir()
    rich = source / "manual.docx"
    _docx(rich, [("heading", "Install"), ("paragraph", "First body"), ("heading", "Deploy"), ("paragraph", "Second body")])

    first = materialize_source("manuals", source, state)
    assert first.stats.added == 1 and first.stats.topics == 2
    output = state / "manuals" / "manual.docx"
    original_tree = _tree(output)
    manifest = load_manifest(output)
    assert manifest.page_count is None
    assert all(document.page_start is None for document in manifest.documents)

    monkeypatch.setattr(
        "docs_mcp.materialize.parse_document",
        lambda *_args, **_kwargs: pytest.fail("unchanged input invoked the parser"),
    )
    second = materialize_source("manuals", source, state)
    assert second.stats.unchanged == 1
    assert _tree(output) == original_tree

    monkeypatch.undo()
    _docx(rich, [("heading", "Install"), ("paragraph", "Replacement body")])
    changed = materialize_source("manuals", source, state)
    assert changed.stats.changed == 1
    assert len(load_manifest(output).documents) == 1
    assert not any("deploy" in name for name in _tree(output))


def test_failed_update_retains_last_known_good_and_is_inspectable(tmp_path):
    source = tmp_path / "source"
    state = tmp_path / "materialized"
    source.mkdir()
    rich = source / "manual.docx"
    _docx(rich, [("heading", "Good"), ("paragraph", "Usable content")])
    materialize_source("manuals", source, state)
    output = state / "manuals" / "manual.docx"
    previous = _tree(output)

    rich.write_bytes(b"not a docx")
    failed = materialize_source("manuals", source, state)
    assert failed.stats.failed == 1
    assert _tree(output) == previous
    inspected = inspect_materialization("manuals", "manual.docx", state)
    assert inspected["origin_sha256"] == load_manifest(output).origin_sha256
    failures = json.loads((state / "manuals" / "_failures.json").read_text())
    assert failures["manual.docx"]["error_code"] == "invalid_signature"

    rich.unlink()
    removed = materialize_source("manuals", source, state)
    assert removed.stats.removed == 1
    assert not output.exists()


def test_manifest_rejects_unlisted_and_unsafe_generated_paths(tmp_path):
    source = tmp_path / "source"
    state = tmp_path / "materialized"
    source.mkdir()
    rich = source / "manual.docx"
    _docx(rich, [("heading", "Safe"), ("paragraph", "Content")])
    materialize_source("manuals", source, state)
    output = state / "manuals" / "manual.docx"
    (output / "extra.md").write_text("not listed")
    with pytest.raises(ValueError, match="do not match"):
        load_manifest(output)
    (output / "extra.md").unlink()

    manifest_path = output / "_manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["documents"][0]["path"] = "../escape.md"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="generated path"):
        load_manifest(output)


def test_page_markers_become_chunk_provenance_not_searchable_text():
    text = """# Topic

<!-- docs-mcp-source-page: 12; label: 10 -->

First page content.

<!-- docs-mcp-source-page: 13; label: 11 -->

Second page content.
"""
    _, chunks = split_materialized_document(text, default_page=12, content_kinds=("paragraph",))
    assert chunks
    assert chunks[0].page_start == 12 and chunks[0].page_end == 13
    assert [item["page"] for item in chunks[0].provenance] == [12, 13]
    assert "docs-mcp-source-page" not in chunks[0].text
    assert "\ue000" not in chunks[0].text


def test_index_snapshot_uses_generated_logical_paths_and_provenance(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "native.md").write_text("# Native\n\nNative body", encoding="utf-8")
    _docx(root / "manual.docx", [("heading", "Deployment"), ("paragraph", "Deploy safely")])
    materialized = tmp_path / "materialized"
    monkeypatch.setattr(
        "docs_mcp.indexer.settings",
        SimpleNamespace(candidate_memory_mb=1, materialized_dir=materialized),
    )
    monkeypatch.setattr(
        "docs_mcp.indexer.embed.embed_passages",
        lambda passages: [np.zeros(2, dtype=np.float32) for _ in passages],
    )
    db = store.connect(tmp_path / "index.db")
    store.create_schema(db, 2)
    source_id = store.upsert_source(
        db,
        name="manuals",
        kind="local",
        origin=str(root),
        ref=None,
        directory=None,
        description=None,
        desired_config_hash="x",
        acquisition_hash="x",
    )
    stats = index_snapshot(
        db,
        SourceSpec("manuals", "local", str(root)),
        root,
        source_id=source_id,
        quiet=True,
    )
    assert stats.failed == 0
    rows = db.execute("SELECT rel_path, origin_path, section_id, page_start FROM files ORDER BY rel_path").fetchall()
    assert any(row["rel_path"] == "native.md" for row in rows)
    generated = [row for row in rows if row["origin_path"] == "manual.docx"]
    assert len(generated) == 1
    assert generated[0]["rel_path"].startswith("manual.docx/d")
    assert generated[0]["rel_path"].endswith("deployment.md")
    assert generated[0]["section_id"] and generated[0]["page_start"] is None
    assert not any(row["rel_path"] == "manual.docx" for row in rows)
    db.close()
