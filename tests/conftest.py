"""Test fixture index.

Env vars are set at import time, before `docs_mcp.config` builds its frozen
settings, so the suite never touches a real /docs or /data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DOCS = REPO_ROOT / "docs"

FIXTURE_ROOT = Path(tempfile.gettempdir()) / "docs-mcp-test-fixture"
FIXTURE_DOCS = FIXTURE_ROOT / "docs"

os.environ.setdefault("DOCS_DIR", str(FIXTURE_DOCS))
os.environ.setdefault("DB_PATH", str(FIXTURE_ROOT / "index.db"))

import pytest  # noqa: E402

# A small slice of the real corpus: enough for meaningful ranking, fast to embed.
FIXTURE_FILES = [
    "rabbitmq-docs/ttl.md",
    "rabbitmq-docs/dlx.md",
    "rabbitmq-docs/consumer-prefetch.md",
    "rabbitmq-docs/vhosts.md",
    "celery-docs/userguide/periodic-tasks.rst",
    "celery-docs/userguide/optimizing.rst",
    "celery-docs/userguide/tasks.rst",
    "celery-docs/userguide/workers.rst",
    "celery-docs/userguide/calling.rst",
    "fastapi-docs/async.md",
    "fastapi-docs/tutorial/background-tasks.md",
    "fastapi-docs/tutorial/dependencies/dependencies-with-yield.md",
    "fastapi-docs/reference/background.md",
    "fastapi-docs/tutorial/middleware.md",
    "velociraptor-docs/docs/clients/artifacts/_index.md",
    "velociraptor-docs/docs/deployment/_index.md",
]


@pytest.fixture(scope="session")
def index_db():
    """Build the fixture index once, then hand out a read-only connection."""
    missing = [f for f in FIXTURE_FILES if not (REAL_DOCS / f).is_file()]
    if missing:
        pytest.skip(
            f"real docs corpus not available (missing {len(missing)} fixture files)"
        )

    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT)
    for rel in FIXTURE_FILES:
        target = FIXTURE_DOCS / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_DOCS / rel, target)

    from docs_mcp import store
    from docs_mcp.config import settings
    from docs_mcp.indexer import reindex

    stats = reindex(quiet=True)
    assert stats.added == len(
        FIXTURE_FILES
    ), f"indexed {stats.added} of {len(FIXTURE_FILES)}"

    db = store.connect(settings.db_path, read_only=True)
    yield db
    db.close()
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)


@pytest.fixture
def fixture_docs(index_db):  # noqa: ARG001 - ordering dependency only
    """Path to the fixture docs tree, for tests that add or remove files."""
    return FIXTURE_DOCS
