#!/usr/bin/env python3
"""
Fetch documentation folders from GitHub repos and store them under dev/<name>-docs/.
Non-text files (images, scripts, binaries, etc.) are removed after download.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent.resolve()

SOURCES = [
    {
        "name": "celery",
        "repo": "https://github.com/celery/celery.git",
        "branch": "main",
        "path": "docs",
        "file_extensions": [".rst"],
    },
    {
        "name": "rabbitmq",
        "repo": "https://github.com/rabbitmq/rabbitmq-website.git",
        "branch": "main",
        "path": "docs",
        "file_extensions": [".md"],
    },
    {
        "name": "velociraptor",
        "repo": "https://github.com/Velocidex/velociraptor-docs.git",
        "branch": "master",
        "path": "content",
        "file_extensions": [".md"],
    },
    {
        "name": "fastapi",
        "repo": "https://github.com/fastapi/fastapi.git",
        "branch": "master",
        "path": "docs/en/docs",
        "file_extensions": [".md"],
    },
]


def run(cmd: list, cwd: Optional[Path] = None) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"  ERROR running {' '.join(cmd)}")
        print(result.stderr.strip())
        sys.exit(1)


def clone_sparse(repo: str, branch: str, sparse_path: str, target_dir: Path) -> Path:
    """Clone only `sparse_path` from `repo` into `target_dir` using sparse checkout."""
    run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth=1",
            "--branch",
            branch,
            repo,
            str(target_dir),
        ]
    )
    run(["git", "sparse-checkout", "init", "--cone"], cwd=target_dir)
    run(["git", "sparse-checkout", "set", sparse_path], cwd=target_dir)
    run(["git", "checkout"], cwd=target_dir)
    return target_dir / sparse_path


def remove_unwanted_files(directory: Path, keep_extensions: set) -> int:
    """Delete every file whose extension is not in keep_extensions, then prune empty dirs."""
    removed = 0
    for file in directory.rglob("*"):
        if file.is_file() and file.suffix.lower() not in keep_extensions:
            file.unlink()
            removed += 1
    # Walk deepest-first so parent dirs become removable after children are gone.
    for dirpath in sorted(directory.rglob("*"), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # only succeeds when the directory is empty
            except OSError:
                pass
    return removed


def fetch_source(source: dict) -> None:
    name = source["name"]
    keep_ext = {e.lower() for e in source["file_extensions"]}
    dest = SCRIPT_DIR / f"{name}-docs"

    print(f"\n{'='*50}")
    print(f"  Fetching:  {name}")
    print(f"  Repo:      {source['repo']}")
    print(f"  Path:      {source['path']}")
    print(f"  Keep:      {', '.join(sorted(keep_ext))}")
    print(f"  Dest:      {dest}")
    print(f"{'='*50}")

    with tempfile.TemporaryDirectory(prefix=f"brakim-docs-{name}-") as tmpdir:
        tmp_path = Path(tmpdir)
        clone_dir = tmp_path / "repo"

        print("  Cloning (sparse)...")
        docs_path = clone_sparse(
            source["repo"],
            source["branch"],
            source["path"],
            clone_dir,
        )

        if not docs_path.exists():
            print(f"  ERROR: Expected path '{docs_path}' not found after clone.")
            sys.exit(1)

        if dest.exists():
            print(f"  Removing old '{dest.name}'...")
            shutil.rmtree(dest)

        print(f"  Copying docs to '{dest.name}'...")
        shutil.copytree(docs_path, dest)

        print(f"  Removing files not matching {sorted(keep_ext)}...")
        removed = remove_unwanted_files(dest, keep_ext)
        print(f"  Removed {removed} file(s).")

    remaining = sum(1 for f in dest.rglob("*") if f.is_file())
    print(f"  Done. {remaining} file(s) kept.")


def main() -> None:
    for source in SOURCES:
        fetch_source(source)
    print("\nAll docs fetched successfully.")


if __name__ == "__main__":
    main()
