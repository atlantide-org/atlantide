"""Fetch, vendor, and hash published components from git.

The three verbs behind the CLI, each keyed by ``alias`` (its local name and the
directory it vendors into):

* :func:`fetch`: clone a :class:`ComponentSource` at its ref, resolve the exact
  commit, copy the package into ``.atlantis/components/<alias>``, and return the
  resolved :class:`LockEntry`.
* :func:`vendor`: rematerialize from a :class:`LockEntry`'s exact commit and assert
  the tree hash matches (rebuild ``.atlantis`` from ``atlantide.lock`` alone).
* :func:`verify`: re-hash the already-vendored tree and compare to the lock
  (tamper/drift check, no network).

The tree hash folds every file's relative path and bytes in sorted order, so it is
deterministic and independent of clone/checkout mechanics, matching the IR hash's
byte-stability. Derived Python caches (``__pycache__``, ``*.pyc``) and the repo's
``.git`` are excluded so they never move the hash.

Symlinks are vendored *as symlinks* and hashed by their target string, never
followed: a component repo is untrusted, and following ``leak -> ~/.aws/credentials``
would copy host files into the project (and into the hash). A link that is
absolute or resolves outside the vendored tree, and any special file (device,
FIFO, socket), is refused outright.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pkgutil
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from atlantide.components._layout import PACKAGE_EXPORTS, components_dir
from atlantide.components.lock import LockEntry
from atlantide.components.source import ComponentSource
from atlantide.core.errors import ComponentError

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")

#: An alias is both a directory name under ``.atlantis/components`` and a Python
#: module name, so it is restricted to an identifier: ``Path`` joining is lexical,
#: and ``../..`` would escape the project before :func:`_swap_in` renames the
#: destination away.
_ALIAS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")

#: Names an alias may not take: ``atlantide.components.<alias>`` would resolve to
#: this package's own submodule (``lock``, ``fetch``, ...) instead of the vendored
#: tree, or importing it would overwrite a package attribute such as ``mount``.
#: Listed from this package's own directory, not ``__path__``, which ``mount``
#: extends with the vendored trees.
_RESERVED_ALIASES = frozenset(
    {m.name for m in pkgutil.iter_modules([str(Path(__file__).parent)])} | PACKAGE_EXPORTS
)

#: Remote forms git may be pointed at. Everything else is rejected, notably
#: ``ext::<cmd>``, which git executes as a shell command.
_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://")
_SCP_LIKE_RE = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:")


def fetch(alias: str, source: ComponentSource, project_root: Path) -> LockEntry:
    """Clone ``source`` at its ref, vendor it, and return the resolved pin."""
    dest = _dest(alias, project_root)
    with _staging(dest) as staged:
        commit = _materialize(source.git, source.ref, source.subdir, staged)
        digest = tree_hash(staged)
        _swap_in(staged, dest)
    return LockEntry(git=source.git, commit=commit, hash=digest, subdir=source.subdir)


def vendor(alias: str, entry: LockEntry, project_root: Path) -> None:
    """Rematerialize an alias from its locked commit and assert the hash matches."""
    dest = _dest(alias, project_root)
    with _staging(dest) as staged:
        _materialize(entry.git, entry.commit, entry.subdir, staged)
        why = f"the source at {entry.commit} changed"
        _assert_hash(alias, tree_hash(staged), entry.hash, why=why)
        _swap_in(staged, dest)


def verify(alias: str, entry: LockEntry, project_root: Path) -> None:
    """Re-hash the vendored tree and compare to the lock (no fetch)."""
    dest = _dest(alias, project_root)
    if not dest.is_dir():
        raise ComponentError(
            f"component {alias!r} is not vendored ({dest}); run `atlantide component vendor`"
        )
    _assert_hash(alias, tree_hash(dest), entry.hash, why="tampered or drifted")


#: Tree-hash format prefix. Every field is length-prefixed so the encoding stays
#: injective when file content contains NUL bytes.
_HASH_PREFIX = "sha256.v2:"


#: Leads a symlink's record in the tree hash. No regular file's path length can
#: equal it, so a link can never collide with a file whose bytes spell its target.
_SYMLINK_TAG = (2**64 - 1).to_bytes(8, "big")


def tree_hash(root: Path) -> str:
    """A deterministic hash over ``root``'s files (length-framed path + bytes).

    A symlink contributes its target string (tagged, see ``_SYMLINK_TAG``) rather
    than whatever it points at, so the hash never reads outside ``root``.
    """
    digest = hashlib.sha256()
    for path in _tree_files(root):
        rel = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            content = os.readlink(path).encode()
            digest.update(_SYMLINK_TAG)
        else:
            content = path.read_bytes()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"{_HASH_PREFIX}{digest.hexdigest()}"


def _dest(alias: str, project_root: Path) -> Path:
    """The vendor dir for ``alias``, proven to sit inside the project.

    Every verb routes through here, so an alias read from ``atlantide.toml`` or
    ``atlantide.lock`` (both untrusted input) is checked before reaching
    the staging dir or the rename in :func:`_swap_in`.
    """
    if not _ALIAS_RE.match(alias):
        raise ComponentError(
            f"component alias {alias!r} is not a valid identifier "
            "(letters, digits, and underscores, starting with a letter)"
        )
    if alias in _RESERVED_ALIASES:
        raise ComponentError(
            f"component alias {alias!r} is reserved: it names part of the "
            "atlantide.components package itself; pick another alias"
        )
    root = components_dir(project_root)
    dest = root / alias
    if not dest.resolve().is_relative_to(root.resolve()):
        raise ComponentError(f"component alias {alias!r} resolves outside {root}")
    return dest


def _assert_hash(alias: str, actual: str, expected: str, *, why: str) -> None:
    if not expected.startswith(_HASH_PREFIX):
        # A lock written by an older build pins a pre-v2 hash; report the format,
        # not a hash mismatch.
        raise ComponentError(
            f"component {alias!r}: lock hash {expected!r} uses an outdated format; "
            "re-run `atlantide component lock` to re-pin it"
        )
    if actual != expected:
        raise ComponentError(
            f"component {alias!r}: vendored tree hashes {actual}, "
            f"but the lock pins {expected} — {why}"
        )


def _tree_files(root: Path) -> list[Path]:
    """Every hashable entry under ``root``, sorted, excluding derived Python caches.

    Regular files and symlinks (to anything, dangling included); symlinked
    directories are listed, not descended into.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            p = base / name
            if not (p.is_symlink() or (name in filenames and p.is_file())):
                continue
            if "__pycache__" not in p.parts and p.suffix != ".pyc":
                found.append(p)
    return sorted(found)


def _check_tree(source: Path) -> None:
    """Refuse a tree whose copy would reach outside it or read a special file.

    Walks exactly what ``copytree`` will copy (same ignore rules). A symlink must
    be relative and resolve inside ``source``, so the vendored copy stays
    self-contained and never points into the host. Anything that is neither a
    regular file, a directory, nor such a link (devices, FIFOs, sockets) is
    refused, since ``copytree`` would block opening a FIFO or read a device.
    """
    root = source.resolve()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        ignored = _IGNORE(dirpath, [*dirnames, *filenames])
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for name in (*dirnames, *filenames):
            if name in ignored:
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(source).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if os.path.isabs(target) or not Path(os.path.realpath(path)).is_relative_to(root):
                    raise ComponentError(
                        f"component file {rel!r} is a symlink to {target!r}, outside the "
                        "component; only relative links within it are vendored"
                    )
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ComponentError(
                    f"component file {rel!r} is not a regular file, directory, or symlink"
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


@contextlib.contextmanager
def _staging(dest: Path) -> Iterator[Path]:
    """A not-yet-existing path to build ``dest``'s replacement at, removed on exit.

    It sits in a temp dir under ``.atlantis`` (the components dir's parent): the
    same filesystem as ``dest``, so :func:`_swap_in` is a rename, but outside the
    mounted components dir, so a leftover from a crash is never importable.
    """
    parent = dest.parent.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".staging-{dest.name}-", dir=parent))
    try:
        yield tmp / "tree"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _swap_in(staged: Path, dest: Path) -> None:
    """Replace ``dest`` with the fully built and checked ``staged`` tree.

    ``os.replace`` cannot overwrite a non-empty directory, so the old tree is
    renamed aside into the staging dir (and removed with it), then the new one
    renamed in; if that second rename fails the old tree is put back. Until the
    swap, a failed fetch/vendor has not touched ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() or dest.is_symlink()):
        os.replace(staged, dest)
        return
    old = staged.with_name("old")
    os.replace(dest, old)
    try:
        os.replace(staged, dest)
    except OSError:
        os.replace(old, dest)
        raise


def _materialize(git: str, ref: str | None, subdir: str | None, dest: Path) -> str:
    """Clone ``git`` at ``ref``, copy ``subdir`` to ``dest``, return the commit sha.

    ``dest`` must not exist yet: callers pass a staging path (see :func:`_staging`).
    """
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
        _check_tree(source)
        shutil.copytree(source, dest, ignore=_IGNORE, symlinks=True)
        return commit


#: Defence in depth alongside :func:`_check_url`: ``ext::<cmd>`` is a git
#: transport whose "url" is a command line. Local-path clones are unaffected.
_GIT_SAFE_CONFIG = ("-c", "protocol.ext.allow=never")


#: Upper bound on one git invocation: room for a large clone over a slow link,
#: but finite so a stalled remote cannot hang the CLI.
GIT_TIMEOUT = 300.0


#: ``git rev-parse --local-env-vars`` (git 2.45): variables that point git at a
#: particular repository, index, object store or config. Inherited from a caller
#: (a git hook, a ``GIT_DIR=... atlantide`` shell), they would redirect the clone
#: and checkout at the caller's repo. Hardcoded rather than asked of git per call.
_GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def _git_env() -> dict[str, str]:
    """The caller's environment, minus every way git could stop to ask a human.

    No stdin reaches git either, so a credential or host-key prompt fails fast
    instead of blocking a non-interactive run. A caller's own ``GIT_SSH_COMMAND``
    / ``GIT_SSH`` / ``GIT_ASKPASS`` is respected; its repo-local variables
    (``_GIT_LOCAL_ENV_VARS``) are dropped.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCAL_ENV_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` and return trimmed stdout; raise :class:`ComponentError` on failure."""
    try:
        proc = subprocess.run(
            ["git", *_GIT_SAFE_CONFIG, *args],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            check=False,
            env=_git_env(),
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise ComponentError("git is required to fetch components but was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ComponentError(
            f"git {args[0]} did not finish within {GIT_TIMEOUT:.0f}s — is the remote "
            "reachable, and does it need credentials? (git is run without prompting)"
        ) from exc
    if proc.returncode != 0:
        raise ComponentError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()
