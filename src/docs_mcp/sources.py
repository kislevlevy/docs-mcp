"""The versioned source configuration and its validation contract."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import settings

CONFIG_VERSION = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_KNOWN_SOURCE_FIELDS = {
    "name",
    "type",
    "url",
    "ref",
    "directory",
    "path",
    "description",
}


class SourceConfigError(ValueError):
    """A user-actionable configuration error which must happen before mutation."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    type: str
    origin: str
    ref: str | None = None
    directory: str | None = None
    description: str | None = None

    @property
    def acquisition_key(self) -> tuple[str, str, str | None, str | None]:
        return self.type, self.origin, self.ref, self.directory

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "origin": self.origin,
            "ref": self.ref,
            "directory": self.directory,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SourceConfig:
    version: int
    sources: tuple[SourceSpec, ...]
    path: Path
    config_hash: str


def _error(message: str, path: Path) -> SourceConfigError:
    return SourceConfigError(
        f"Configuration error: {message}\nFile: {path}\nNo sources were changed."
    )


def _relative_git_directory(value: Any, entry: int, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(
            f"source entry {entry}: directory must be a non-empty relative path", path
        )
    if "\\" in value:
        raise _error(f"source entry {entry}: directory must use forward slashes", path)
    candidate = Path(value)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise _error(
            f"source entry {entry}: directory must stay inside the checkout", path
        )
    return candidate.as_posix()


def _source(raw: Any, entry: int, path: Path) -> SourceSpec:
    if not isinstance(raw, dict):
        raise _error(f"source entry {entry} must be a table", path)
    unknown = sorted(set(raw) - _KNOWN_SOURCE_FIELDS)
    if unknown:
        raise _error(
            f"source entry {entry}: unknown field(s): {', '.join(unknown)}", path
        )
    name = raw.get("name")
    kind = raw.get("type")
    reserved_key = name.casefold().split(".", 1)[0] if isinstance(name, str) else ""
    if (
        not isinstance(name, str)
        or not _NAME_RE.fullmatch(name)
        or name.endswith(".")
        or reserved_key in _RESERVED_NAMES
    ):
        raise _error(
            f"source entry {entry}: name must match [a-z0-9][a-z0-9._-]*", path
        )
    if kind not in {"git", "local"}:
        raise _error(
            f"source entry {entry}: type must be exactly 'git' or 'local'", path
        )
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise _error(f"source entry {entry}: description must be a string", path)

    if kind == "git":
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _error(f"source entry {entry}: git sources require url", path)
        parsed_url = urlsplit(url.strip())
        if parsed_url.scheme in {"http", "https"} and (
            parsed_url.username is not None or parsed_url.query or parsed_url.fragment
        ):
            raise _error(
                f"source entry {entry}: git URL must not contain credentials", path
            )
        if "path" in raw:
            raise _error(f"source entry {entry}: git sources cannot define path", path)
        ref = raw.get("ref")
        if ref is not None and (not isinstance(ref, str) or not ref.strip()):
            raise _error(f"source entry {entry}: ref must be a non-empty string", path)
        return SourceSpec(
            name,
            kind,
            url.strip(),
            ref.strip() if ref else None,
            _relative_git_directory(raw.get("directory"), entry, path),
            description,
        )

    local_path = raw.get("path")
    if not isinstance(local_path, str) or not local_path.strip():
        raise _error(f"source entry {entry}: local sources require path", path)
    if any(key in raw for key in ("url", "ref", "directory")):
        raise _error(
            f"source entry {entry}: local sources only accept path and description",
            path,
        )
    origin = Path(local_path).expanduser().resolve()
    if not origin.is_dir():
        raise _error(
            f"source entry {entry}: local path is not a directory: {origin}", path
        )
    return SourceSpec(name, kind, str(origin), None, None, description)


def load(path: Path | None = None, *, required: bool = True) -> SourceConfig | None:
    """Parse and completely validate sources.toml before a sync mutates anything."""
    path = (path or settings.sources_config).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        if required:
            raise _error("sources.toml was not found", path)
        return None
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _error(f"could not read TOML: {exc}", path) from exc
    if set(raw) - {"version", "source"}:
        raise _error(
            f"unknown top-level field(s): {', '.join(sorted(set(raw) - {'version', 'source'}))}",
            path,
        )
    if type(raw.get("version")) is not int or raw.get("version") != CONFIG_VERSION:
        raise _error(
            f"unsupported version {raw.get('version')!r}; expected {CONFIG_VERSION}",
            path,
        )
    entries = raw.get("source", [])
    if not isinstance(entries, list):
        raise _error("source must be an array of tables", path)
    specs = tuple(_source(value, i, path) for i, value in enumerate(entries, 1))
    seen: dict[str, int] = {}
    for i, spec in enumerate(specs, 1):
        key = spec.name.casefold()
        if key in seen:
            raise _error(
                f'duplicate source name "{spec.name}" (defined by entries {seen[key]} and {i})',
                path,
            )
        seen[key] = i
    legacy_seen: dict[str, int] = {}
    for i, spec in enumerate(specs, 1):
        key = spec.name.removesuffix("-docs")
        if key in legacy_seen:
            raise _error(
                f'old "-docs" normalization collision between entries {legacy_seen[key]} and {i}',
                path,
            )
        legacy_seen[key] = i
    canonical = {
        "version": CONFIG_VERSION,
        "source": [spec.canonical() for spec in specs],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SourceConfig(CONFIG_VERSION, specs, path, digest)


def source_hash(spec: SourceSpec) -> str:
    payload = json.dumps(
        spec.canonical(), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def acquisition_hash(spec: SourceSpec) -> str:
    payload = json.dumps(spec.acquisition_key, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
