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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from atlantide.components._layout import components_dir
from atlantide.components.lock import LockEntry
from atlantide.components.source import ComponentSource
from atlantide.core.errors import ComponentError

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")

#: An alias is both a directory name under ``.atlantis/components`` and a Python
#: module name, so it is restricted to an identifier: ``Path`` joining is lexical,
#: and ``../..`` would escape the project before :func:`_materialize` ``rmtree``s
#: the destination.
_ALIAS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")

#: Remote forms git may be pointed at. Everything else is rejected, notably
#: ``ext::<cmd>``, which git executes as a shell command.
_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://")
_SCP_LIKE_RE = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:")


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


#: Current tree-hash format. ``v2`` length-prefixes every field: the original
#: ``path\0content\0`` concatenation was not injective (file content may itself
#: contain NUL bytes), so distinct trees could collide by construction —
#: defeating the tamper check the hash exists for.
_HASH_PREFIX = "sha256.v2:"


def tree_hash(root: Path) -> str:
    """A deterministic hash over ``root``'s files (length-framed path + bytes)."""
    digest = hashlib.sha256()
    for path in _tree_files(root):
        rel = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"{_HASH_PREFIX}{digest.hexdigest()}"


def _dest(alias: str, project_root: Path) -> Path:
    """The vendor dir for ``alias``, proven to sit inside the project.

    Every verb routes through here, so an alias read from ``atlantide.toml`` or
    ``atlantide.lock`` — neither of which is trusted input — is checked once,
    before reaching ``rmtree``/``copytree``.
    """
    if not _ALIAS_RE.match(alias):
        raise ComponentError(
            f"component alias {alias!r} is not a valid identifier "
            "(letters, digits, and underscores, starting with a letter)"
        )
    root = components_dir(project_root)
    dest = root / alias
    if not dest.resolve().is_relative_to(root.resolve()):
        raise ComponentError(f"component alias {alias!r} resolves outside {root}")
    return dest


def _assert_hash(alias: str, actual: str, expected: str, *, why: str) -> None:
    if not expected.startswith(_HASH_PREFIX):
        # Loud and specific rather than a misleading "tampered" mismatch: a lock
        # written by an older build pins a hash in the pre-v2 format.
        raise ComponentError(
            f"component {alias!r}: lock hash {expected!r} uses an outdated format; "
            "re-run `atlantide component fetch` to re-pin it"
        )
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


def _check_url(git: str) -> None:
    """Reject remotes git would read as an option or as a command to run."""
    if git.startswith("-"):
        raise ComponentError(f"component url {git!r} would be read by git as an option")
    if "::" in git:
        raise ComponentError(
            f"component url {git!r} uses a git remote helper (`<transport>::<cmd>`), "
            "which executes a command; use an https/ssh url or a local path"
        )
    if git.startswith(_URL_SCHEMES) or _SCP_LIKE_RE.match(git) or Path(git).is_absolute():
        return
    raise ComponentError(
        f"component url {git!r} is not an https/http/ssh/git/file url, an scp-like "
        "`user@host:path`, or an absolute local path"
    )


def _check_ref(ref: str) -> None:
    if ref.startswith("-"):
        raise ComponentError(f"component ref {ref!r} would be read by git as an option")


def _subdir_path(repo: Path, subdir: str) -> Path:
    """``repo/subdir``, proven not to escape the clone.

    ``subdir`` is untrusted input, and a lexical join would walk out of the temp
    clone and vendor an arbitrary directory into the project.
    """
    source = repo / subdir
    if not source.resolve().is_relative_to(repo.resolve()):
        raise ComponentError(f"subdir {subdir!r} resolves outside the component repo")
    return source


def _materialize(git: str, ref: str | None, subdir: str | None, dest: Path) -> str:
    """Clone ``git`` at ``ref``, copy ``subdir`` to ``dest``, return the commit sha."""
    _check_url(git)
    if ref:
        _check_ref(ref)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _git("clone", "--quiet", "--", git, str(repo))
        if ref:
            _git("checkout", "--quiet", ref, "--", cwd=repo)
        commit = _git("rev-parse", "HEAD", cwd=repo)
        source = _subdir_path(repo, subdir) if subdir else repo
        if not source.is_dir():
            raise ComponentError(f"subdir {subdir!r} not found in {git} at {ref or 'HEAD'}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=_IGNORE)
        return commit


#: Defence in depth alongside :func:`_check_url`: ``ext::<cmd>`` is a git
#: transport whose "url" is a command line. Local-path clones are unaffected.
_GIT_SAFE_CONFIG = ("-c", "protocol.ext.allow=never")


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` and return trimmed stdout; raise :class:`ComponentError` on failure."""
    try:
        proc = subprocess.run(
            ["git", *_GIT_SAFE_CONFIG, *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:  # git not installed
        raise ComponentError("git is required to fetch components but was not found") from exc
    if proc.returncode != 0:
        raise ComponentError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()
