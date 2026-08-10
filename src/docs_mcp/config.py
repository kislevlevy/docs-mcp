"""Environment-driven settings. Every knob has a working default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Extensions we treat as documentation. Anything else in docs/ is ignored.
DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt"})


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # Paths
    docs_dir: Path = field(default_factory=lambda: Path(os.environ.get("DOCS_DIR", "/docs")))
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("DB_PATH", "/data/index.db")))

    # Serving
    host: str = os.environ.get("HOST", "0.0.0.0")  # noqa: S104 - published address is set by compose
    port: int = int(os.environ.get("PORT", "8765"))
    mcp_path: str = os.environ.get("MCP_PATH", "/mcp")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Access control. Empty auth_token disables bearer auth (network-restricted deploy).
    auth_token: str = os.environ.get("AUTH_TOKEN", "")
    allowed_origins: list[str] = field(default_factory=lambda: _csv("ALLOWED_ORIGINS"))
    allowed_hosts: list[str] = field(default_factory=lambda: _csv("ALLOWED_HOSTS"))

    # Models
    dense_model: str = os.environ.get("DENSE_MODEL", "BAAI/bge-small-en-v1.5")
    rerank_model: str = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
    # Off by default: measured on this corpus, the cross-encoder gave no improvement
    # on prose queries (fusion already ranks 6 of 7 golden prose queries first) while
    # costing 150-300ms per search. Set RERANK=1 to enable it for a corpus where it
    # does help; identifier queries bypass it either way. See tests/test_retrieval.py.
    rerank: bool = field(default_factory=lambda: _flag("RERANK", False))
    embed_batch: int = int(os.environ.get("EMBED_BATCH", "64"))
    threads: int | None = int(os.environ["THREADS"]) if os.environ.get("THREADS") else None

    # Retrieval shape
    candidates: int = int(os.environ.get("CANDIDATES", "50"))  # per leg (BM25, dense)
    rerank_pool: int = int(os.environ.get("RERANK_POOL", "40"))  # fused hits sent to reranker
    default_limit: int = int(os.environ.get("DEFAULT_LIMIT", "8"))
    rrf_k: int = int(os.environ.get("RRF_K", "60"))
    # Fusion weights. The phrase leg only fires on identifier-shaped queries, where
    # an exact adjacency match is near ground truth, so it outvotes the fuzzy legs.
    w_phrase: float = float(os.environ.get("W_PHRASE", "3.0"))
    w_lexical: float = float(os.environ.get("W_LEXICAL", "1.0"))
    w_dense: float = float(os.environ.get("W_DENSE", "1.0"))
    # Reranker vs retrieval when blending the final order.
    w_retrieval: float = float(os.environ.get("W_RETRIEVAL", "1.0"))
    w_rerank: float = float(os.environ.get("W_RERANK", "1.5"))

    # Chunking, in characters (~4 chars per token)
    chunk_target: int = int(os.environ.get("CHUNK_TARGET", "4000"))
    chunk_max: int = int(os.environ.get("CHUNK_MAX", "8000"))
    chunk_min: int = int(os.environ.get("CHUNK_MIN", "160"))
    # A whole document under this is a redirect stub (a bare path, "CVE-1234.html")
    # with nothing retrievable in it. Measured against the corpus: everything below
    # 40 chars is a stub, while the 40-160 band is real short content worth keeping.
    stub_max: int = int(os.environ.get("STUB_MAX", "40"))


settings = Settings()
