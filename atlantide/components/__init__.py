"""Published components: git-pinned, vendored locally, imported from config.

A published component is a reusable L2 construct (a
:class:`~atlantide.core.Component` subclass) shared in a git repo, declared under
``[components.<alias>]`` in ``atlantide.toml``, pinned in ``atlantide.lock`` and
imported from config as ``atlantide.components.<alias>``. ``README.md`` explains
why fetching is a separate, pinned step and what the pin vouches for.

:func:`mount` extends this package's ``__path__`` with the vendored trees, so the
interpreter's ``importlib.import_module`` resolves ``atlantide.components.<alias>``
and the sandbox's allowed-prefix rule applies to it unchanged.

Layout (a hidden, derived dir in the project root; git-ignore it)::

    <project>/.atlantis/components/<alias>/   # the vendored package tree
    <project>/atlantide.lock                  # resolved commit + hash pins
"""

from __future__ import annotations

from pathlib import Path

from atlantide.components._layout import VENDOR_DIR, components_dir

__all__ = ["VENDOR_DIR", "components_dir", "mount", "verify_vendored"]


def mount(project_root: Path, *, verify: bool = True) -> None:
    """Make vendored components importable as ``atlantide.components.<alias>``.

    Appends the project's ``.atlantis/components`` dir to this package's
    ``__path__``, so the interpreter's
    ``importlib.import_module("atlantide.components.<alias>")`` resolves the
    vendored subpackage. Idempotent; a no-op when nothing is vendored.

    With ``verify``, each locked alias is first re-hashed against
    ``atlantide.lock``: mounting makes third-party Python importable, so the pin
    is checked here, not only in ``atlantide component verify``, to keep
    ``plan``/``apply``/``build`` from running a tampered or stale tree. Only
    commands that evaluate config mount; the component commands, which rebuild
    the tree, do not.
    """
    root = components_dir(project_root)
    entry = str(root)
    if not root.is_dir():
        return
    if verify:
        verify_vendored(project_root)
    if entry not in __path__:
        __path__.append(entry)


def verify_vendored(project_root: Path) -> None:
    """Re-hash every vendored alias against ``atlantide.lock``.

    An alias in the lock but absent from disk is not vendored yet and is skipped;
    the import reports it. A directory on disk with no lock entry is refused:
    :func:`mount` makes every directory under ``.atlantis/components`` importable,
    so an unlocked one would run third-party Python without hash verification.
    Any other entry (a file, a symlink) with an importable name is refused too:
    ``acme.py``, ``acme.pyc`` or ``acme.<abi>.so`` dropped in place of a locked
    ``acme/`` would import as ``atlantide.components.acme`` without ever being
    hashed. Entries no import can reach (``.DS_Store``, ``.gitkeep``, ``my-pkg``)
    are ignored.

    Each locked tree's bytecode (``__pycache__``, ``*.pyc``) is deleted before
    mounting. The tree hash excludes it, yet Python loads a cached ``.pyc`` whose
    header matches the source's mtime and size (and a bare ``mod.pyc`` with no
    source at all), so a forged cache would run in place of the verified source.
    Python regenerates it from the verified source on the next import.
    """
    # Lazy imports: config imports this package, and the fetcher (which shells out
    # to git) and the lockfile reader stay off its import path until a mount.
    from atlantide.components.fetch import verify as verify_alias
    from atlantide.components.lock import LOCKFILE, load_lock
    from atlantide.core.errors import ComponentError

    root = components_dir(project_root)
    locked = load_lock(project_root)
    entries = [p for p in root.iterdir() if _importable(p)] if root.is_dir() else []
    # `is_symlink` first: `is_dir` follows links, and a linked alias dir would be
    # hashed and purged through the link, outside the project.
    stray = sorted(p.name for p in entries if p.is_symlink() or not p.is_dir())
    if stray:
        names = ", ".join(repr(n) for n in stray)
        raise ComponentError(
            f"{root} holds {names}, which is not a directory; only vendored component "
            "directories may live there (anything else is importable without a hash "
            "check), so remove it and run `atlantide component vendor`"
        )
    unlocked = sorted(
        p.name for p in entries if p.is_dir() and p.name != "__pycache__" and p.name not in locked
    )
    if unlocked:
        names = ", ".join(repr(n) for n in unlocked)
        raise ComponentError(
            f"vendored component(s) {names} have no entry in {LOCKFILE}; "
            "remove the directory or re-run `atlantide component lock` to pin it"
        )
    for alias, entry in locked.items():
        if (root / alias).is_dir():
            verify_alias(alias, entry, project_root)
            _purge_bytecode(root / alias)


def _importable(entry: Path) -> bool:
    """Whether ``entry`` could import as ``atlantide.components.<x>``.

    Import finders match a directory on its whole name and a file on the part
    before the first ``.`` (``acme.py``, ``acme.cpython-312-darwin.so``); only an
    identifier that is not a keyword can be imported. A symlink is judged like a
    file (the stricter reading), since it may point at either.
    """
    import keyword

    is_dir = entry.is_dir() and not entry.is_symlink()
    name = entry.name if is_dir else entry.name.split(".", 1)[0]
    return name.isidentifier() and not keyword.iskeyword(name)


def _purge_bytecode(tree: Path) -> None:
    """Delete every ``__pycache__`` and ``*.pyc`` under ``tree`` (links unlinked, not followed)."""
    # Lazy, like the imports above: this package's namespace is importable from
    # config, so `os`/`shutil` must not become attributes of it.
    import os
    import shutil

    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            if name == "__pycache__":
                dirnames.remove(name)
                path = base / name
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        for name in filenames:
            if name == "__pycache__" or name.endswith(".pyc"):
                (base / name).unlink()
