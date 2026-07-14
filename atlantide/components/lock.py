"""``atlantide.lock`` — the resolved pins for published components.

Where ``[components.<alias>]`` in ``atlantide.toml`` says *what* to fetch (a git
repo + a requested ref), the lock records the *resolved truth*: the exact commit
and a content hash of the vendored tree. It is the reproducibility contract —
``vendor``/``verify`` rematerialize and re-check against it, mirroring how a
``.atlas`` artifact pins provider versions.

Generated, not hand-edited. Stdlib reads TOML but cannot write it, so the small,
fixed shape here is emitted by hand; values (git URLs, hex commits, ``sha256:``
hashes) never contain a double quote, so no escaping is needed.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

LOCKFILE = "atlantide.lock"

_HEADER = "# atlantide.lock — resolved component pins (generated; do not edit by hand)\n\n"


@dataclass(frozen=True)
class LockEntry:
    """One alias's resolved pin — everything needed to rematerialize it offline:
    the repo, the exact commit, the package ``subdir``, and the tree hash."""

    git: str
    commit: str
    hash: str  # "sha256:<hex>" over the vendored tree; see components.fetch
    subdir: str | None = None


def lock_path(project_root: Path) -> Path:
    return project_root / LOCKFILE


def load_lock(project_root: Path) -> dict[str, LockEntry]:
    """Read ``atlantide.lock``; returns ``{}`` when absent. Malformed entries are
    skipped (a partial lock still mounts the entries that are well-formed)."""
    path = lock_path(project_root)
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        tables = tomllib.load(fh).get("components")
    if not isinstance(tables, dict):
        return {}
    return {
        alias: _entry_from_toml(body)
        for alias, body in tables.items()
        if _is_lock_entry(body)
    }


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


def _entry_to_toml(alias: str, entry: LockEntry) -> str:
    lines = [
        f"[components.{alias}]",
        f'git = "{entry.git}"',
        f'commit = "{entry.commit}"',
        f'hash = "{entry.hash}"',
    ]
    if entry.subdir is not None:
        lines.append(f'subdir = "{entry.subdir}"')
    return "\n".join(lines) + "\n"
