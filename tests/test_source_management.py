"""Regression tests for configured source acquisition and publication."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from docs_mcp import store
from docs_mcp.formats import walk_supported
from docs_mcp.indexer import index_snapshot
from docs_mcp.sources import SourceConfig, SourceConfigError, SourceSpec, load
from docs_mcp.sync import _boundary_check, sync


def _write_config(path: Path, body: str) -> None:
    path.write_text("version = 1\n\n" + body, encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        '[[source]]\nname="one"\ntype="git"\nurl="https://secret@github.com/acme/docs.git"\n',
        '[[source]]\nname="one"\ntype="git"\nurl="https://example.com/docs.git"\ndirectory="docs\\\\outside"\n',
        (
            '[[source]]\nname="one"\ntype="git"\nurl="https://example.com/one.git"\n\n'
            '[[source]]\nname="one-docs"\ntype="git"\nurl="https://example.com/two.git"\n'
        ),
    ],
)
def test_configuration_rejects_credential_path_and_legacy_collisions(tmp_path, body):
    config = tmp_path / "sources.toml"
    _write_config(config, body)
    with pytest.raises(SourceConfigError, match="No sources were changed"):
        load(config)


def test_walk_skips_hidden_unsupported_and_symlinked_entries(tmp_path):
    (tmp_path / "visible.md").write_text("ok")
    (tmp_path / "archive.zip").write_bytes(b"zip")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("hidden")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(hidden, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    files, skipped = walk_supported(tmp_path, "local")
    assert [path.name for path in files] == ["visible.md"]
    assert skipped == 3


def _source(db, name: str) -> int:
    return store.upsert_source(
        db,
        name=name,
        kind="local",
        origin="/origin",
        ref=None,
        directory=None,
        description=None,
        desired_config_hash="desired",
        acquisition_hash="acquisition",
        sync_status="success",
        index_status="ready",
    )


def _chunk(text: str):
    return SimpleNamespace(ord=0, heading_path="Title", text=text)


def test_failed_candidate_preserves_last_known_good_source(tmp_path, monkeypatch):
    db = store.connect(tmp_path / "index.db")
    store.create_schema(db, 2)
    source_id = _source(db, "handbook")
    store.upsert_file(
        db,
        source="handbook",
        source_id=source_id,
        rel_path="good.md",
        sha256="old",
        title="Old",
        size=3,
        chunks=[_chunk("last known good")],
        vectors=[np.zeros(2, dtype=np.float32)],
    )
    db.commit()

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "good.md").write_text("replacement")
    (snapshot / "bad.txt").write_bytes(b"text\x00binary")
    monkeypatch.setattr(
        "docs_mcp.indexer.embed.embed_passages",
        lambda passages: [np.zeros(2, dtype=np.float32) for _ in passages],
    )

    stats = index_snapshot(
        db,
        SourceSpec("handbook", "local", "/origin"),
        snapshot,
        source_id=source_id,
        quiet=True,
    )

    assert stats.failed == 1
    assert "bad.txt" in (stats.error or "")
    row = store.get_document(db, "handbook", "good.md")
    assert row is not None and row["sha256"] == "old"
    assert store.document_text(db, int(row["id"])) == "last known good"
    db.close()


def test_fts_source_filter_is_part_of_match_expression(tmp_path):
    db = store.connect(tmp_path / "index.db")
    store.create_schema(db, 2)
    for name in ("alpha", "beta"):
        source_id = _source(db, name)
        store.upsert_file(
            db,
            source=name,
            source_id=source_id,
            rel_path="same.md",
            sha256=name,
            title=name,
            size=1,
            chunks=[_chunk("shared rare phrase")],
            vectors=[np.zeros(2, dtype=np.float32)],
        )
    db.commit()

    ids = store._match(db, '"shared"', ["alpha"], 10)
    rows = store._load(db, ids)
    assert ids and {row["source"] for row in rows.values()} == {"alpha"}
    db.close()


def test_source_removal_clears_relational_fts_and_vector_rows(tmp_path):
    db = store.connect(tmp_path / "index.db")
    store.create_schema(db, 2)
    source_id = _source(db, "remove-me")
    store.upsert_file(
        db,
        source="remove-me",
        source_id=source_id,
        rel_path="doc.md",
        sha256="x",
        title="Doc",
        size=1,
        chunks=[_chunk("content to remove")],
        vectors=[np.zeros(2, dtype=np.float32)],
    )
    db.commit()

    assert store.delete_source(db, "remove-me") == (1, 1)
    db.commit()
    for table in ("sources", "files", "chunks", "chunks_fts", "chunks_vec"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    db.close()


def test_missing_read_only_database_behaves_as_empty(tmp_path):
    db = store.connect(tmp_path / "missing.db", read_only=True)
    assert not store.schema_ready(db)
    assert not (tmp_path / "missing.db").exists()
    db.close()


def test_legacy_schema_file_updates_remain_compatible(tmp_path):
    db = store.connect(tmp_path / "legacy.db")
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, rel_path TEXT NOT NULL,
            sha256 TEXT NOT NULL, title TEXT, bytes INTEGER NOT NULL,
            indexed_at TEXT NOT NULL, UNIQUE(source, rel_path)
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, ord INTEGER NOT NULL,
            heading_path TEXT NOT NULL DEFAULT '', text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, text, heading_path
        );
        CREATE VIRTUAL TABLE chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY, source TEXT PARTITION KEY,
            embedding FLOAT[2]
        );
        """)
    for sha, text in (("old", "old text"), ("new", "new text")):
        store.upsert_file(
            db,
            source="legacy",
            rel_path="doc.md",
            sha256=sha,
            title="Doc",
            size=1,
            chunks=[_chunk(text)],
            vectors=[np.zeros(2, dtype=np.float32)],
        )
        db.commit()
    row = store.get_document(db, "legacy", "doc.md")
    assert row is not None and row["sha256"] == "new"
    assert store.document_text(db, int(row["id"])) == "new text"
    db.close()


def test_database_path_cannot_modify_a_local_origin(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    config_path = tmp_path / "sources.toml"
    _write_config(
        config_path,
        f'[[source]]\nname="local"\ntype="local"\npath="{origin}"\n',
    )
    config = load(config_path)
    assert config is not None
    monkeypatch.setattr(
        "docs_mcp.sync.settings",
        SimpleNamespace(db_path=origin / "index.db"),
    )
    with pytest.raises(ValueError, match="DB_PATH"):
        _boundary_check(config, tmp_path / "state")


def test_dry_run_rebuild_and_invalid_scope_do_not_create_state(tmp_path, monkeypatch):
    spec = SourceSpec("git-source", "git", "https://example.com/docs.git")
    config = SourceConfig(1, (spec,), tmp_path / "sources.toml", "hash")
    state = tmp_path / "state"
    monkeypatch.setattr("docs_mcp.sync.load", lambda: config)
    monkeypatch.setattr(
        "docs_mcp.sync.settings",
        SimpleNamespace(db_path=tmp_path / "index.db", state_dir=state),
    )

    result = sync(rebuild=True, dry_run=True, quiet=True)
    assert [(row.name, row.sync) for row in result.sources] == [
        ("git-source", "rebuild")
    ]
    with pytest.raises(ValueError, match="database-wide"):
        sync(source="git-source", rebuild=True, quiet=True)
    assert not state.exists()
