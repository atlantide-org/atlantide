"""``atlantide.lock``: the resolved pins for published components.

``[components.<alias>]`` in ``atlantide.toml`` names what to fetch (a git repo and
a requested ref); the lock records the exact commit and a content hash of the
vendored tree. ``vendor``/``verify`` rematerialize and re-check against it, as a
``.atlas`` artifact pins provider versions.

Generated, not hand-edited. The stdlib reads TOML but cannot write it, so the
fixed shape here is emitted by hand, with every value escaped by
:func:`toml_string`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from atlantide.core.errors import ComponentError

LOCKFILE = "atlantide.lock"

_HEADER = "# atlantide.lock — resolved component pins (generated; do not edit by hand)\n\n"


@dataclass(frozen=True)
class LockEntry:
    """One alias's resolved pin, sufficient to rematerialize it offline."""

    git: str
    commit: str
    hash: str  # "sha256.v2:<hex>" over the vendored tree; see components.fetch.tree_hash
    subdir: str | None = None


def lock_path(project_root: Path) -> Path:
    return project_root / LOCKFILE


def load_lock(project_root: Path) -> dict[str, LockEntry]:
    """Read ``atlantide.lock``; returns ``{}`` when absent.

    A malformed entry raises rather than being skipped: a skipped alias would stay
    mounted and importable without hash verification.
    """
    path = lock_path(project_root)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            tables = tomllib.load(fh).get("components")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ComponentError(
            f"{LOCKFILE} is not valid TOML ({exc}); "
            "re-run `atlantide component lock` to regenerate it"
        ) from exc
    if not isinstance(tables, dict):
        return {}
    for alias, body in tables.items():
        if not _is_lock_entry(body):
            raise ComponentError(
                f"component {alias!r}: malformed entry in {LOCKFILE} (needs string "
                "git/commit/hash); re-run `atlantide component lock` to regenerate it"
            )
    return {alias: _entry_from_toml(body) for alias, body in tables.items()}


def write_lock(project_root: Path, entries: dict[str, LockEntry]) -> None:
    """Write ``atlantide.lock`` with aliases sorted for a stable, diffable file."""
    blocks = [_entry_to_toml(alias, entries[alias]) for alias in sorted(entries)]
    lock_path(project_root).write_text(_HEADER + "\n".join(blocks))


def _is_lock_entry(body: object) -> bool:
    """A well-formed ``[components.<alias>]`` table has string git/commit/hash."""
    return isinstance(body, dict) and all(
        isinstance(body.get(key), str) for key in ("git", "commit", "hash")
    )


def _entry_from_toml(body: dict[str, object]) -> LockEntry:
    subdir = body.get("subdir")
    return LockEntry(
        git=str(body["git"]),
        commit=str(body["commit"]),
        hash=str(body["hash"]),
        subdir=subdir if isinstance(subdir, str) else None,
    )


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string, escaped.

    Values arrive from a git URL or a ``--ref`` flag; an unescaped quote or
    newline closes the string early and injects arbitrary TOML, such as a
    ``[state]`` table redirecting the project at another backend.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _entry_to_toml(alias: str, entry: LockEntry) -> str:
    lines = [
        f"[components.{alias}]",
        f"git = {toml_string(entry.git)}",
        f"commit = {toml_string(entry.commit)}",
        f"hash = {toml_string(entry.hash)}",
    ]
    if entry.subdir is not None:
        lines.append(f"subdir = {toml_string(entry.subdir)}")
    return "\n".join(lines) + "\n"
