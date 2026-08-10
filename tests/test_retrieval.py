"""Retrieval quality gate, run against a real (small) index.

These are the tests that fail if chunking, the query prefix, fusion or reranking
silently regress - the failure modes that produce a server that still "works"
but returns the wrong passages.
"""

from __future__ import annotations

import pytest

from docs_mcp import embed, store
from docs_mcp.config import settings

# (query, expected document, why it is here)
GOLDEN = [
    ("how do I set a per-message TTL", "rabbitmq/ttl.md", "semantic phrasing"),
    ("dead letter exchange", "rabbitmq/dlx.md", "domain term"),
    (
        "dependency with yield cleanup after the response",
        "fastapi/tutorial/dependencies/dependencies-with-yield.md",
        "long question",
    ),
    (
        "run a task on a schedule periodically",
        "celery/userguide/periodic-tasks.rst",
        "paraphrase",
    ),
    (
        "prefetch_count",
        "celery/userguide/optimizing.rst",
        "bare identifier - exercises the phrase and BM25 legs",
    ),
    (
        "acks_late",
        "celery/userguide/tasks.rst",
        "identifier that tokenizes into two common words",
    ),
    ("BackgroundTasks", "fastapi/reference/background.md", "exact API name"),
]


def where(hits):
    return [f"{h.source}/{h.path}" for h in hits]


@pytest.mark.parametrize(
    ("query", "expected", "why"), GOLDEN, ids=[g[0][:28] for g in GOLDEN]
)
def test_golden_query_lands_in_top_three(index_db, query, expected, why):
    hits = store.search(index_db, query, limit=3)
    assert expected in where(hits), f"[{why}] {expected} missing from {where(hits)}"


def test_identifier_query_is_found_without_the_dense_leg(index_db):
    """An exact identifier must be reachable lexically, not only by embedding."""
    ids = store._lexical(index_db, "prefetch_count", None, 20)
    assert ids, "BM25 leg returned nothing for a bare identifier"


def test_semantic_query_is_found_without_the_lexical_leg(index_db):
    """A paraphrase with no shared vocabulary must be reachable by embedding."""
    ids = store._dense(
        index_db, "cleaning up resources once a response has been sent", None, 20
    )
    rows = store._load(index_db, ids)
    assert any("dependencies-with-yield" in r["rel_path"] for r in rows.values())


def test_source_filter_restricts_results(index_db):
    hits = store.search(
        index_db, "how do I set a per-message TTL", sources=["celery"], limit=5
    )
    assert hits, "source-filtered search returned nothing"
    assert {h.source for h in hits} == {"celery"}


def test_multiple_source_filter_spans_partitions(index_db):
    hits = store.search(
        index_db, "retry a failed message", sources=["celery", "rabbitmq"], limit=8
    )
    assert {h.source for h in hits} <= {"celery", "rabbitmq"}
    assert hits


def test_retrieval_quality_floor(index_db):
    """Mean reciprocal rank over the golden set must not regress.

    This is the gate that catches a silent retrieval regression - one where the
    server still answers, just with worse passages.
    """
    reciprocal = []
    for query, expected, _ in GOLDEN:
        found = where(store.search(index_db, query, limit=10))
        rank = found.index(expected) + 1 if expected in found else 0
        reciprocal.append(1 / rank if rank else 0.0)
    mrr = sum(reciprocal) / len(reciprocal)
    assert (
        mrr >= 0.85
    ), f"MRR regressed to {mrr:.3f}: {list(zip([g[0] for g in GOLDEN], reciprocal))}"


def test_identifier_queries_bypass_the_reranker(index_db):
    """A cross-encoder scores bare identifiers as uniformly irrelevant, so its
    ordering is noise there. Enabling reranking must not change those results."""
    for query in ("prefetch_count", "acks_late", "x-death header"):
        assert store.phrase_query(query) is not None
        assert where(store.search(index_db, query, limit=5, rerank=True)) == where(
            store.search(index_db, query, limit=5, rerank=False)
        )


def test_reranking_prose_stays_functional(index_db):
    """Reranking is off by default but must still work when switched on."""
    query = "how do I clean up a resource after the response is sent"
    assert store.phrase_query(query) is None  # prose, so the reranker does apply
    hits = store.search(index_db, query, limit=5, rerank=True)
    assert hits and all(h.text for h in hits)


def test_phrase_leg_pins_exact_identifiers(index_db):
    """`acks_late` tokenizes to `acks`+`late`; only the phrase leg finds it precisely."""
    assert store.phrase_query("acks_late") == '"acks late"'
    assert store.phrase_query("x-death") == '"x death"'
    assert store.phrase_query("how do I set a TTL") is None
    ids = store._match(index_db, store.phrase_query("acks_late"), None, 10)
    rows = store._load(index_db, ids)
    assert any("tasks.rst" in r["rel_path"] for r in rows.values())


def test_query_instruction_is_applied_to_queries_only():
    """BGE is asymmetric: the instruction belongs on queries, never on passages."""
    q = embed.embed_query("per-message ttl")
    plain = embed.embed_passages(["per-message ttl"])[0]
    assert q.shape == plain.shape
    assert not (
        q == plain
    ).all(), "query embedding is identical to the passage embedding"


def test_fts_query_survives_punctuation():
    """Raw `:`/`-`/`*` in a MATCH expression is a syntax error, not a no-op."""
    for raw in [
        "what is x-death?",
        "config: broker_url",
        "queue*",
        '"unbalanced',
        "a AND b OR NOT c",
    ]:
        assert store.fts_query(raw), f"no MATCH expression built for {raw!r}"
    assert store.fts_query("!!!") is None


def test_punctuation_heavy_query_does_not_raise(index_db):
    hits = store.search(index_db, "what does x-death: do (exactly)?", limit=3)
    assert isinstance(hits, list)


def test_empty_and_junk_queries_return_cleanly(index_db):
    assert store.search(index_db, "!!!", limit=3) == []


def test_hits_carry_everything_needed_to_fetch_more(index_db):
    hits = store.search(index_db, "dead letter exchange", limit=3)
    for hit in hits:
        assert hit.chunk_id > 0
        assert hit.source and hit.path and hit.text
        assert store.get_chunk(index_db, hit.chunk_id, context=1)
        assert store.get_document(index_db, hit.source, hit.path) is not None


def _orphans(db) -> tuple[int, int]:
    lexical = db.execute(
        "SELECT COUNT(*) AS n FROM chunks_fts WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).fetchone()["n"]
    dense = db.execute(
        "SELECT COUNT(*) AS n FROM chunks_vec WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).fetchone()["n"]
    return lexical, dense


def test_file_lifecycle_add_touch_edit_remove(index_db, fixture_docs):
    """The whole incremental contract, on a file this test owns start to finish.

    `index_db` is a separate read-only connection to the same database, so this
    also proves a reader sees the indexer's writes without coordination.
    """
    from docs_mcp.indexer import reindex

    target = fixture_docs / "rabbitmq-docs/lifecycle-probe.md"
    target.write_text(
        "# Lifecycle Probe\n\n" + "Text about zzqqxx frobnication limits. " * 40
    )

    # add
    stats = reindex(quiet=True)
    assert (stats.added, stats.changed, stats.removed) == (1, 0, 0)
    assert store.get_document(index_db, "rabbitmq", "lifecycle-probe.md") is not None
    assert (
        where(store.search(index_db, "zzqqxx frobnication", limit=3))[0]
        == "rabbitmq/lifecycle-probe.md"
    )

    # touch: mtime moves, bytes do not -> no work
    target.touch()
    stats = reindex(quiet=True)
    assert (stats.added, stats.changed) == (0, 0), "mtime change triggered re-embedding"
    assert stats.chunks == 0

    # edit: bytes change -> exactly one file re-embedded
    target.write_text(
        target.read_text() + "\n\nA further note about wibblewobble tuning.\n"
    )
    stats = reindex(quiet=True)
    assert (stats.added, stats.changed) == (0, 1)
    assert stats.chunks > 0
    # Findable, not necessarily rank 1: the cross-encoder has no semantic signal for
    # invented words, so it can outrank a rare-token match. Real identifiers still
    # win outright - see the prefetch_count golden query.
    assert "rabbitmq/lifecycle-probe.md" in where(
        store.search(index_db, "wibblewobble tuning", limit=5)
    )

    # remove: gone from the index, no orphans left in either table
    target.unlink()
    stats = reindex(quiet=True)
    assert stats.removed == 1
    assert store.get_document(index_db, "rabbitmq", "lifecycle-probe.md") is None
    assert "rabbitmq/lifecycle-probe.md" not in where(
        store.search(index_db, "zzqqxx frobnication", limit=10)
    )
    assert _orphans(index_db) == (0, 0)


def test_absent_topics_still_return_neighbours(index_db):
    """Documents the deliberate limit of vector search rather than papering over it.

    A KNN always has neighbours, and measured on this corpus the best-match distance
    for a genuine paraphrase (0.80) overlaps the best-match distance for an invented
    term (0.83). That gap is too small to threshold without silently losing recall on
    real paraphrase queries, so no cutoff is applied: callers get ranked candidates
    plus a score, not a relevance guarantee.
    """
    hits = store.search(index_db, "photosynthesis in C4 plants", limit=3)
    assert hits, "expected neighbours rather than an empty result"
    # A query with no searchable token at all is still refused outright.
    assert store.search(index_db, "!!!", limit=3) == []


def test_dropping_a_whole_source_directory_clears_it(index_db, fixture_docs):
    import shutil

    from docs_mcp.indexer import reindex

    probe = fixture_docs / "throwaway-docs"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "note.md").write_text(
        "# Throwaway\n\n" + "Notes about qqzzvv handling. " * 40
    )
    reindex(quiet=True)
    assert store.get_document(index_db, "throwaway", "note.md") is not None

    shutil.rmtree(probe)
    stats = reindex(quiet=True)
    assert stats.removed == 1
    assert "throwaway" not in {s["source"] for s in store.list_sources(index_db)}
    assert _orphans(index_db) == (0, 0)
