"""Fetch, vendor, and hash published components from git.

The three verbs behind the CLI, each keyed by ``alias`` (its local name and the
directory it vendors into):

* :func:`fetch` — clone a :class:`ComponentSource` at its ref, resolve the exact
  commit, copy the package into ``.atlantis/components/<alias>``, and return the
  resolved :class:`LockEntry`.
* :func:`vendor` — rematerialize from a :class:`LockEntry`'s exact commit and assert
  the tree hash matches (rebuild ``.atlantis`` from ``atlantide.lock`` alone).
* :func:`verify` — re-hash the already-vendored tree and compare to the lock
  (tamper/drift check, no network).

The tree hash folds every file's relative path and bytes in sorted order, so it is
deterministic and independent of clone/checkout mechanics, matching the IR hash's
byte-stability. Derived Python caches (``__pycache__``, ``*.pyc``) and the repo's
``.git`` are excluded so they never move the hash.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from atlantide.components import components_dir
from atlantide.components.lock import LockEntry
from atlantide.components.source import ComponentSource
from atlantide.core.errors import ComponentError

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")


def fetch(alias: str, source: ComponentSource, project_root: Path) -> LockEntry:
    """Clone ``source`` at its ref, vendor it, and return the resolved pin."""
    dest = _dest(alias, project_root)
    commit = _materialize(source.git, source.ref, source.subdir, dest)
    return LockEntry(git=source.git, commit=commit, hash=tree_hash(dest), subdir=source.subdir)


def vendor(alias: str, entry: LockEntry, project_root: Path) -> None:
    """Rematerialize an alias from its locked commit and assert the hash matches."""
    dest = _dest(alias, project_root)
    _materialize(entry.git, entry.commit, entry.subdir, dest)
    _assert_hash(alias, tree_hash(dest), entry.hash, why=f"the source at {entry.commit} changed")


def verify(alias: str, entry: LockEntry, project_root: Path) -> None:
    """Re-hash the vendored tree and compare to the lock (no fetch)."""
    dest = _dest(alias, project_root)
    if not dest.is_dir():
        raise ComponentError(
            f"component {alias!r} is not vendored ({dest}); run `atlantide component vendor`"
        )
    _assert_hash(alias, tree_hash(dest), entry.hash, why="tampered or drifted")


def tree_hash(root: Path) -> str:
    """A deterministic ``sha256:`` hash over ``root``'s files (path + bytes)."""
    digest = hashlib.sha256()
    for path in _tree_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _dest(alias: str, project_root: Path) -> Path:
    return components_dir(project_root) / alias


def _assert_hash(alias: str, actual: str, expected: str, *, why: str) -> None:
    if actual != expected:
        raise ComponentError(
            f"component {alias!r}: vendored tree hashes {actual}, "
            f"but the lock pins {expected} — {why}"
        )


def _tree_files(root: Path) -> list[Path]:
    """Every hashable file under ``root``, sorted, excluding derived Python caches."""
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )


def _materialize(git: str, ref: str | None, subdir: str | None, dest: Path) -> str:
    """Clone ``git`` at ``ref``, copy ``subdir`` to ``dest``, return the commit sha."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _git("clone", "--quiet", git, str(repo))
        if ref:
            _git("checkout", "--quiet", ref, cwd=repo)
        commit = _git("rev-parse", "HEAD", cwd=repo)
        source = repo / subdir if subdir else repo
        if not source.is_dir():
            raise ComponentError(f"subdir {subdir!r} not found in {git} at {ref or 'HEAD'}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=_IGNORE)
        return commit


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` and return trimmed stdout; raise :class:`ComponentError` on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:  # git not installed
        raise ComponentError("git is required to fetch components but was not found") from exc
    if proc.returncode != 0:
        raise ComponentError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()
