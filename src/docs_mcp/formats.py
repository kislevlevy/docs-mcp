"""Supported file registry and source-kind eligibility policy."""

from __future__ import annotations

import os
from pathlib import Path

from .config import DOC_EXTENSIONS, RICH_DOCUMENT_EXTENSIONS

TEXT_PARSERS = frozenset(DOC_EXTENSIONS)
RICH_DOCUMENT_PARSERS = frozenset(RICH_DOCUMENT_EXTENSIONS)


def eligible_suffix(suffix: str, source_type: str) -> bool:
    suffix = suffix.lower()
    return suffix in TEXT_PARSERS or (
        source_type == "local" and suffix in RICH_DOCUMENT_PARSERS
    )


def walk_supported(root: Path, source_type: str) -> tuple[list[Path], int]:
    """Return regular supported files, skipping hidden paths and symlinks."""
    files: list[Path] = []
    skipped = 0
    for directory, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        kept_directories = [
            d
            for d in dirnames
            if not d.startswith(".") and not (Path(directory) / d).is_symlink()
        ]
        skipped += len(dirnames) - len(kept_directories)
        dirnames[:] = sorted(kept_directories)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if filename.startswith(".") or path.is_symlink():
                skipped += 1
                continue
            if eligible_suffix(path.suffix, source_type):
                files.append(path)
            else:
                skipped += 1
    return files, skipped
