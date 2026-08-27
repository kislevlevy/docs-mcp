"""The public source reconciliation workflow behind ``docs-mcp sync``."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import embed, store
from .acquire import Snapshot, acquire
from .config import settings
from .indexer import index_snapshot
from .sources import SourceConfig, SourceSpec, acquisition_hash, load, source_hash


@dataclass(frozen=True, slots=True)
class SourceResult:
    name: str
    sync: str
    index: str
    result: str
    files: int = 0
    chunks: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    sources: tuple[SourceResult, ...]
    failed: bool = False
    dry_run: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def _boundary_check(config: SourceConfig, state_dir: Path) -> None:
    state = state_dir.expanduser().resolve()
    database = settings.db_path.expanduser().resolve()
    for spec in config.sources:
        if spec.type != "local":
            continue
        origin = Path(spec.origin).resolve()
        try:
            state.relative_to(origin)
            inside = True
        except ValueError:
            inside = False
        try:
            origin.relative_to(state)
            contains = True
        except ValueError:
            contains = False
        if inside or contains:
            raise ValueError(
                f"STATE_DIR must not contain or be contained by local source '{spec.name}'"
            )
        try:
            database.relative_to(origin)
        except ValueError:
            pass
        else:
            raise ValueError(f"DB_PATH must not be inside local source '{spec.name}'")


@contextmanager
def _lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "locks" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _existing_names(db) -> set[str]:
    if not store.schema_ready(db):
        return set()
    names = {row["name"] for row in store.source_rows(db)}
    names.update(
        row["source"] for row in db.execute("SELECT DISTINCT source FROM files")
    )
    return names


def _plan(config: SourceConfig, db, only: str | None) -> list[tuple[str, str]]:
    existing = _existing_names(db)
    wanted = {spec.name for spec in config.sources}
    selected = [spec for spec in config.sources if only is None or spec.name == only]
    rows = {row["name"]: row for row in store.source_rows(db)}
    plan: list[tuple[str, str]] = []
    for spec in selected:
        row = rows.get(spec.name)
        if spec.name not in existing:
            action = "add"
        elif row is None:
            action = "migrate"
        elif row["sync_status"] == "failed":
            action = "retry"
        elif row["acquisition_hash"] != acquisition_hash(spec):
            action = "update"
        elif row["desired_config_hash"] != source_hash(spec):
            action = "metadata"
        else:
            action = "unchanged"
        plan.append((spec.name, action))
    if only is None:
        plan.extend((name, "remove") for name in sorted(existing - wanted))
    return plan


def _counts(db, name: str) -> tuple[int, int]:
    row = db.execute(
        """SELECT COUNT(DISTINCT f.id) AS files, COUNT(c.id) AS chunks
           FROM files f LEFT JOIN chunks c ON c.file_id=f.id WHERE f.source=?""",
        (name,),
    ).fetchone()
    return int(row["files"]), int(row["chunks"])


def _safe_error(spec: SourceSpec, exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        if detail:
            message = f"{message}: {detail[-1]}"
    message = message.replace(spec.origin, "<origin>")
    return message[:500] or type(exc).__name__


def _progress(name: str, percent: int, message: str, *, quiet: bool) -> None:
    if not quiet:
        print(f"[{name}] {percent:3d}% {message}", flush=True)


def _progress_callback(name: str, *, quiet: bool):
    last_percent = -1

    def report(percent: int, message: str) -> None:
        nonlocal last_percent
        if percent != last_percent:
            last_percent = percent
            _progress(name, percent, message, quiet=quiet)

    return report


def _ensure_source(db, spec: SourceSpec) -> int:
    old = db.execute("SELECT * FROM sources WHERE name = ?", (spec.name,)).fetchone()
    source_id = store.upsert_source(
        db,
        name=spec.name,
        kind=spec.type,
        origin=spec.origin,
        ref=spec.ref,
        directory=spec.directory,
        description=spec.description,
        desired_config_hash=source_hash(spec),
        acquisition_hash=acquisition_hash(spec),
        sync_status="pending",
        index_status=old["index_status"] if old else "absent",
        indexed_files=int(old["indexed_files"]) if old else 0,
        indexed_chunks=int(old["indexed_chunks"]) if old else 0,
    )
    # Compatibility databases may have files from the old discovery workflow.
    # Associate them without changing their content or embeddings.
    db.execute(
        "UPDATE files SET source_id=? WHERE source=? AND (source_id IS NULL OR source_id != ?)",
        (source_id, spec.name, source_id),
    )
    return source_id


def _remove_source_state(state_dir: Path, name: str) -> None:
    """Remove only this source's managed cache and staging directories."""
    root = state_dir.resolve()
    managed = [state_dir / "staging" / name, state_dir / "git-cache" / name]
    managed.extend((state_dir / "tmp").glob(f"{name}-*"))
    for raw_path in managed:
        path = raw_path.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError("refusing to clean a source path outside STATE_DIR")
        if path.exists():
            shutil.rmtree(path)


def _cleanup_managed_state(state_dir: Path, wanted: set[str]) -> None:
    """Remove orphaned cache/staging entries, including state from an older sync run."""
    root = state_dir.resolve()

    def remove_managed(raw_path: Path) -> None:
        path = raw_path.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError("refusing to clean a managed path outside STATE_DIR")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    for folder_name in ("git-cache", "staging"):
        folder = state_dir / folder_name
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.name not in wanted:
                remove_managed(entry)
                continue
            # Staging contains only temporary snapshots. A successful or failed
            # attempt must not leave old snapshots behind; Git cache is retained.
            if folder_name == "staging" and entry.is_dir():
                remove_managed(entry)

    tmp = state_dir / "tmp"
    if tmp.is_dir():
        for entry in tmp.iterdir():
            remove_managed(entry)


def _remove_snapshot(state_dir: Path, snapshot: Snapshot) -> None:
    root = state_dir.resolve()
    path = snapshot.path.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError("refusing to clean a snapshot outside STATE_DIR")
    if path.exists():
        shutil.rmtree(path)


def _run_incremental(
    config: SourceConfig, *, only: str | None, rebuild: bool, quiet: bool
) -> SyncResult:
    db_path = settings.db_path.expanduser().resolve()
    state_dir = settings.state_dir.expanduser().resolve()
    db = store.connect(db_path)
    stored_pipeline = store.get_meta(db, "pipeline_fingerprint")
    if (
        stored_pipeline is None
        and store.get_meta(db, "schema_version") is not None
        and store.schema_ready(db)
        and db.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None
    ):
        db.close()
        raise RuntimeError(
            "indexing pipeline metadata is missing; run 'docs-mcp sync --rebuild'"
        )
    if stored_pipeline is not None and stored_pipeline != store.pipeline_fingerprint():
        db.close()
        raise RuntimeError("indexing pipeline changed; run 'docs-mcp sync --rebuild'")
    stored_dim = store.get_meta(db, "dim")
    dim = int(stored_dim) if stored_dim else embed.dimension()
    store.create_schema(db, dim)
    planned = _plan(config, db, only)
    if not quiet:
        for name, action in planned:
            print(f"  {action:6} {name}")

    results: list[SourceResult] = []
    if only is None:
        wanted = {spec.name for spec in config.sources}
        for name in sorted(_existing_names(db) - wanted):
            files, chunks = store.delete_source(db, name)
            db.commit()
            _remove_source_state(state_dir, name)
            results.append(
                SourceResult(
                    name,
                    "removed",
                    "absent",
                    f"removed {files} files and {chunks} chunks",
                    files,
                    chunks,
                )
            )

    specs = [spec for spec in config.sources if only is None or spec.name == only]
    for spec in specs:
        progress = _progress_callback(spec.name, quiet=quiet)
        old = db.execute("SELECT * FROM sources WHERE name=?", (spec.name,)).fetchone()
        source_id = _ensure_source(db, spec)
        # A metadata-only edit is observable immediately and must not reacquire or
        # re-embed the source. Failed sources are retried even when their origin is unchanged.
        if (
            old is not None
            and not rebuild
            and old["sync_status"] == "success"
            and old["index_status"] == "ready"
            and old["acquisition_hash"] == acquisition_hash(spec)
            and old["desired_config_hash"] != source_hash(spec)
        ):
            store.mark_source_attempt(
                db,
                spec.name,
                status="success",
                index_status="ready",
                config_hash=source_hash(spec),
                revision=old["indexed_revision"],
            )
            db.commit()
            files, chunks = _counts(db, spec.name)
            results.append(
                SourceResult(
                    spec.name, "success", "ready", "metadata updated", files, chunks
                )
            )
            continue
        db.commit()
        progress(0, "acquiring source")
        try:
            snapshot = acquire(spec, state_dir)
        except Exception as exc:
            message = _safe_error(spec, exc)
            old = db.execute(
                "SELECT index_status FROM sources WHERE name=?", (spec.name,)
            ).fetchone()
            index_status = old["index_status"] if old else "absent"
            store.mark_source_attempt(
                db,
                spec.name,
                status="failed",
                index_status=index_status,
                error_code="acquire_failed",
                error_message=message,
            )
            db.commit()
            results.append(
                SourceResult(spec.name, "failed", index_status, message, error=message)
            )
            continue
        eligible_files = sum(1 for path in snapshot.path.rglob("*") if path.is_file())
        progress(10, f"acquired {eligible_files} files")
        stats = index_snapshot(
            db,
            spec,
            snapshot.path,
            source_id=source_id,
            force=rebuild,
            quiet=quiet,
            progress=progress,
        )
        if stats.failed:
            _remove_snapshot(state_dir, snapshot)
            old = db.execute(
                "SELECT index_status FROM sources WHERE name=?", (spec.name,)
            ).fetchone()
            index_status = old["index_status"] if old else "absent"
            store.mark_source_attempt(
                db,
                spec.name,
                status="failed",
                index_status=index_status,
                error_code="index_failed",
                error_message=(stats.error or "source candidate was rejected")[:500],
            )
            db.commit()
            message = stats.error or "source candidate was rejected"
            results.append(
                SourceResult(spec.name, "failed", index_status, message, error=message)
            )
            continue
        files, chunks = _counts(db, spec.name)
        store.mark_source_attempt(
            db,
            spec.name,
            status="success",
            index_status="ready",
            config_hash=source_hash(spec),
            revision=snapshot.revision,
        )
        db.execute(
            "UPDATE sources SET indexed_files=?, indexed_chunks=? WHERE name=?",
            (files, chunks, spec.name),
        )
        # Content rows and the ready status become visible in one commit.
        db.commit()
        progress(100, "ready")
        action = (
            "unchanged"
            if not (stats.added or stats.changed or stats.removed)
            else "updated"
        )
        revision = f" to commit {snapshot.revision[:8]}" if snapshot.revision else ""
        skipped = snapshot.skipped + stats.skipped
        suffix = f", {skipped} skipped" if skipped else ""
        results.append(
            SourceResult(
                spec.name,
                "success",
                "ready",
                f"{action}{revision}{suffix}",
                files,
                chunks,
                skipped,
            )
        )
        _remove_snapshot(state_dir, snapshot)
    _cleanup_managed_state(state_dir, {spec.name for spec in config.sources})
    db.execute("PRAGMA optimize")
    db.close()
    return SyncResult(tuple(results), any(row.sync == "failed" for row in results))


def _run_rebuild(config: SourceConfig, *, quiet: bool) -> SyncResult:
    """Build beside the active DB and publish only after every source succeeds."""
    db_path = settings.db_path.expanduser().resolve()
    state_dir = settings.state_dir.expanduser().resolve()
    snapshots: list[Snapshot] = []
    results: list[SourceResult] = []
    previous_status: dict[str, str] = {}
    if db_path.exists():
        previous = store.connect(db_path, read_only=True)
        previous_status.update(
            {row["name"]: row["index_status"] for row in store.source_rows(previous)}
        )
        for name in store.known_sources(previous):
            previous_status.setdefault(name, "ready")
        previous.close()
    for spec in config.sources:
        progress = _progress_callback(spec.name, quiet=quiet)
        progress(0, "acquiring source")
        try:
            snapshots.append(acquire(spec, state_dir))
        except Exception as exc:
            message = _safe_error(spec, exc)
            index_status = previous_status.get(spec.name, "absent")
            results.append(
                SourceResult(spec.name, "failed", index_status, message, error=message)
            )
            for snapshot in snapshots:
                _remove_snapshot(state_dir, snapshot)
            return SyncResult(tuple(results), True)
        snapshot = snapshots[-1]
        eligible_files = sum(1 for path in snapshot.path.rglob("*") if path.is_file())
        progress(10, f"acquired {eligible_files} files")

    # A rebuild is also the migration path for a changed embedding model, whose
    # output width may differ from the active database.
    dim = embed.dimension()
    next_path = db_path.with_name(db_path.name + ".next")
    if next_path.exists():
        next_path.unlink()
    for suffix in ("", "-wal", "-shm"):
        artifact = Path(str(next_path) + suffix)
        if artifact.exists():
            artifact.unlink()
    candidate = store.connect(next_path)
    store.create_schema(candidate, dim)
    published = True
    try:
        for snapshot in snapshots:
            spec = snapshot.source
            progress = _progress_callback(spec.name, quiet=quiet)
            source_id = _ensure_source(candidate, spec)
            stats = index_snapshot(
                candidate,
                spec,
                snapshot.path,
                source_id=source_id,
                force=True,
                quiet=quiet,
                progress=progress,
            )
            if stats.failed:
                published = False
                message = stats.error or "index candidate rejected"
                index_status = previous_status.get(spec.name, "absent")
                results.append(
                    SourceResult(
                        spec.name, "failed", index_status, message, error=message
                    )
                )
                break
            files, chunks = _counts(candidate, spec.name)
            store.mark_source_attempt(
                candidate,
                spec.name,
                status="success",
                index_status="ready",
                config_hash=source_hash(spec),
                revision=snapshot.revision,
            )
            candidate.execute(
                "UPDATE sources SET indexed_files=?, indexed_chunks=? WHERE name=?",
                (files, chunks, spec.name),
            )
            candidate.commit()
            progress(100, "ready")
            revision = (
                f" to commit {snapshot.revision[:8]}" if snapshot.revision else ""
            )
            suffix = f", {snapshot.skipped} skipped" if snapshot.skipped else ""
            results.append(
                SourceResult(
                    spec.name,
                    "success",
                    "ready",
                    f"rebuilt{revision}{suffix}",
                    files,
                    chunks,
                    snapshot.skipped,
                )
            )
        if published:
            store.validate_database(candidate)
            candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            candidate.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        published = False
        raise
    finally:
        candidate.close()
        for snapshot in snapshots:
            _remove_snapshot(state_dir, snapshot)
    if not published:
        for suffix in ("", "-wal", "-shm"):
            artifact = Path(str(next_path) + suffix)
            if artifact.exists():
                artifact.unlink()
        return SyncResult(tuple(results), True)

    backup = db_path.with_name(db_path.name + ".previous")
    if backup.exists():
        backup.unlink()
    if db_path.exists():
        # A database file must never be replaced while a WAL belonging to the old
        # inode is still named index.db-wal. Make the old database standalone first.
        active = store.connect(db_path)
        active.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        active.execute("PRAGMA journal_mode=DELETE")
        active.close()
        os.replace(db_path, backup)
    try:
        os.replace(next_path, db_path)
    except Exception:
        if backup.exists():
            os.replace(backup, db_path)
        raise
    published_db = None
    try:
        published_db = store.connect(db_path, read_only=True)
        store.validate_database(published_db)
    except Exception:
        if db_path.exists():
            db_path.unlink()
        if backup.exists():
            os.replace(backup, db_path)
        raise
    finally:
        if published_db is not None:
            published_db.close()
    return SyncResult(tuple(results), False)


def sync(
    *,
    source: str | None = None,
    rebuild: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> SyncResult:
    config = load()
    assert config is not None
    if source and source not in {spec.name for spec in config.sources}:
        raise ValueError(
            f"Unknown source {source!r}; configured sources: {', '.join(spec.name for spec in config.sources) or '(none)'}"
        )
    requires_rebuild = rebuild
    if settings.db_path.exists():
        existing = store.connect(settings.db_path, read_only=True)
        schema_version = store.get_meta(existing, "schema_version")
        pipeline = store.get_meta(existing, "pipeline_fingerprint")
        has_schema = store.schema_ready(existing)
        has_content = (
            has_schema
            and existing.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None
        )
        existing.close()
        requires_rebuild = requires_rebuild or (
            (has_schema and schema_version is None)
            or (
                schema_version is not None
                and (
                    schema_version != str(store.SCHEMA_VERSION)
                    or (pipeline is None and has_content)
                    or (
                        pipeline is not None
                        and pipeline != store.pipeline_fingerprint()
                    )
                )
            )
        )
    if requires_rebuild and source:
        raise ValueError(
            "--rebuild is database-wide and cannot be combined with --source"
        )
    _boundary_check(config, settings.state_dir)
    if dry_run:
        if requires_rebuild:
            plan = [(spec.name, "rebuild") for spec in config.sources]
        elif not settings.db_path.exists():
            plan = [
                (spec.name, "add")
                for spec in config.sources
                if source is None or spec.name == source
            ]
        else:
            db = store.connect(settings.db_path, read_only=True)
            plan = _plan(config, db, source)
            db.close()
        if not quiet:
            print("Sync plan (dry run):")
            for name, action in plan:
                print(f"  {action:6} {name}")
        return SyncResult(
            tuple(
                SourceResult(name, action, "unknown", "planned")
                for name, action in plan
            ),
            dry_run=True,
        )
    with _lock(settings.state_dir):
        if requires_rebuild:
            return _run_rebuild(config, quiet=quiet)
        return _run_incremental(config, only=source, rebuild=False, quiet=quiet)


def format_report(result: SyncResult) -> str:
    lines = ["SOURCE       SYNC      INDEX   RESULT"]
    for row in result.sources:
        message = row.result.replace("\n", " ")
        lines.append(f"{row.name:<12} {row.sync:<9} {row.index:<7} {message}")
    if not result.sources:
        lines.append("(no sources configured)")
    if result.failed:
        lines.append(
            "Sync completed with failures; previous usable indexes were retained where available."
        )
    return "\n".join(lines)
