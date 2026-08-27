"""MCP tool-layer tests: what the AI model actually calls."""

from __future__ import annotations

import asyncio

import pytest

from docs_mcp import server


def run(coro):
    """Drive a tool coroutine without depending on an async pytest plugin."""
    return asyncio.run(coro)


def test_list_sources_reports_every_indexed_source(
    index_db,
):  # noqa: ARG001 - builds the index
    result = run(server.list_sources())
    names = {s.source for s in result.sources}
    assert {"celery", "fastapi", "rabbitmq", "velociraptor"} <= names
    assert result.total_files == sum(s.files for s in result.sources)
    assert result.total_chunks == sum(s.chunks for s in result.sources)


def test_search_restricted_to_one_source(index_db):  # noqa: ARG001
    """Every public search is scoped to exactly one source."""
    result = run(
        server.search_docs("how do I set a per-message TTL", source="celery", limit=5)
    )
    assert result.hits, "expected celery-only hits"
    assert {h.source for h in result.hits} == {"celery"}


def test_unknown_source_is_rejected_with_the_valid_list(index_db):  # noqa: ARG001
    """A silent empty result would leave the model unable to tell a typo from a miss."""
    with pytest.raises(ValueError, match="Unknown source") as excinfo:
        run(server.search_docs("ttl", source="celry"))
    message = str(excinfo.value)
    assert "celry" in message
    assert "celery" in message and "list_sources" in message


def test_fetch_chunk_returns_neighbours(index_db):  # noqa: ARG001
    hits = run(
        server.search_docs("dead letter exchange", source="rabbitmq", limit=1)
    ).hits
    result = run(server.fetch_chunk("rabbitmq", hits[0].chunk_id, context=1))
    assert len(result.passages) >= 2
    assert any(p.chunk_id == hits[0].chunk_id for p in result.passages)


def test_fetch_chunk_is_source_scoped(index_db):  # noqa: ARG001
    hits = run(
        server.search_docs("dead letter exchange", source="rabbitmq", limit=1)
    ).hits
    with pytest.raises(ValueError, match="No such chunk_id"):
        run(server.fetch_chunk("celery", hits[0].chunk_id, context=1))


def test_fetch_doc_paginates(index_db):  # noqa: ARG001
    first = run(
        server.fetch_doc("celery", "userguide/periodic-tasks.rst", max_chars=1000)
    )
    assert first.next_offset == 1000
    assert len(first.text) == 1000
    rest = run(
        server.fetch_doc(
            "celery",
            "userguide/periodic-tasks.rst",
            offset=first.next_offset,
            max_chars=200_000,
        )
    )
    assert rest.next_offset is None
    assert len(first.text) + len(rest.text) == first.total_chars


def test_fetch_doc_rejects_unknown_and_traversal_paths(index_db):  # noqa: ARG001
    for bad in ("../../etc/passwd", "does-not-exist.md"):
        with pytest.raises(ValueError, match="No such document"):
            run(server.fetch_doc("celery", bad))


def test_resource_serves_a_nested_path(index_db):  # noqa: ARG001
    text = run(server.doc_resource("celery", "userguide/periodic-tasks.rst"))
    assert len(text) > 1000
