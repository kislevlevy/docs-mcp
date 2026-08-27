"""Acquire immutable, filtered snapshots without touching configured origins."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .formats import eligible_suffix
from .sources import SourceSpec


@dataclass(frozen=True, slots=True)
class Snapshot:
    source: SourceSpec
    path: Path
    revision: str | None
    skipped: int = 0


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _copy_filtered(origin: Path, target: Path, kind: str) -> int:
    skipped = 0
    for directory, dirnames, filenames in os.walk(
        origin, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        kept_directories = [
            d
            for d in dirnames
            if not d.startswith(".") and not (directory_path / d).is_symlink()
        ]
        skipped += len(dirnames) - len(kept_directories)
        dirnames[:] = sorted(kept_directories)
        relative = directory_path.relative_to(origin)
        for filename in sorted(filenames):
            source = directory_path / filename
            if (
                filename.startswith(".")
                or source.is_symlink()
                or not eligible_suffix(source.suffix, kind)
            ):
                skipped += 1
                continue
            destination = target / relative / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return skipped


def _publish_snapshot(source: Path, staging_root: Path) -> Path:
    """Return a snapshot only after its private staging directory is complete."""
    published = Path(tempfile.mkdtemp(prefix="snapshot-", dir=staging_root))
    try:
        shutil.copytree(source, published, dirs_exist_ok=True)
    except Exception:
        shutil.rmtree(published, ignore_errors=True)
        raise
    return published


def _git_snapshot(spec: SourceSpec, state_dir: Path) -> Snapshot:
    cache = state_dir / "git-cache" / spec.name
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / ".git").exists():
        subprocess.run(
            ["git", "init", "--quiet", str(cache)],
            check=True,
            capture_output=True,
            text=True,
        )
    remote = subprocess.run(
        ["git", "-C", str(cache), "remote"], check=True, capture_output=True, text=True
    ).stdout.split()
    if "origin" not in remote:
        subprocess.run(
            ["git", "-C", str(cache), "remote", "add", "origin", spec.origin],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(cache), "remote", "set-url", "origin", spec.origin],
            check=True,
            capture_output=True,
            text=True,
        )
    # Fetch HEAD explicitly when no ref was configured. Fetching only the remote
    # name can populate several FETCH_HEAD entries and makes rev-parse ambiguous.
    fetch_args = [
        "git",
        "-C",
        str(cache),
        "fetch",
        "--depth=1",
        "--no-tags",
        "origin",
        spec.ref or "HEAD",
    ]
    subprocess.run(fetch_args, check=True, capture_output=True, text=True)
    revision = subprocess.run(
        ["git", "-C", str(cache), "rev-parse", "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(
        prefix=f"{spec.name}-", dir=state_dir / "tmp"
    ) as temp:
        target = Path(temp) / "snapshot"
        target.mkdir()
        archive_args = [
            "git",
            "-C",
            str(cache),
            "archive",
            "--format=tar",
            "FETCH_HEAD",
        ]
        if spec.directory:
            archive_args.append(spec.directory)
        result = subprocess.run(archive_args, check=True, capture_output=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            prefix = (spec.directory.rstrip("/") + "/") if spec.directory else ""
            skipped = 0
            for member in archive.getmembers():
                if not member.name.startswith(prefix) or member.isdir():
                    continue
                relative = Path(member.name[len(prefix) :])
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise ValueError(
                        f"Git archive contains an unsafe path: {member.name}"
                    )
                if (
                    not member.isfile()
                    or not eligible_suffix(relative.suffix, "git")
                    or any(part.startswith(".") for part in relative.parts)
                ):
                    skipped += 1
                    continue
                destination = target / relative
                if not _inside(destination, target):
                    raise ValueError(
                        f"Git archive contains an unsafe path: {member.name}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is not None:
                    destination.write_bytes(extracted.read())
            published = _publish_snapshot(target, state_dir / "staging" / spec.name)
            return Snapshot(spec, published, revision, skipped)


def acquire(spec: SourceSpec, state_dir: Path) -> Snapshot:
    state_dir = state_dir.expanduser().resolve()
    (state_dir / "tmp").mkdir(parents=True, exist_ok=True)
    (state_dir / "staging").mkdir(parents=True, exist_ok=True)
    (state_dir / "staging" / spec.name).mkdir(parents=True, exist_ok=True)
    if spec.type == "git":
        return _git_snapshot(spec, state_dir)
    origin = Path(spec.origin).resolve()
    if _inside(state_dir, origin) or _inside(origin, state_dir):
        raise ValueError(
            "STATE_DIR must not contain or be contained by a local source path"
        )
    with tempfile.TemporaryDirectory(
        prefix=f"{spec.name}-", dir=state_dir / "tmp"
    ) as temp:
        target = Path(temp) / "snapshot"
        target.mkdir()
        skipped = _copy_filtered(origin, target, "local")
        published = _publish_snapshot(target, state_dir / "staging" / spec.name)
        return Snapshot(spec, published, None, skipped)
