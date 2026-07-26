"""Published components: git-pinned, vendored locally, imported from config.

A *published component* is a reusable L2 construct (a :class:`~atlantide.core.Component`
subclass) that someone shares in a public git repo. Others declare it under
``[components.<alias>]`` in ``atlantide.toml``, fetch it once (pinned to a commit +
content hash in ``atlantide.lock``), and import it from Atlas-lang config as
``atlantide.components.<alias>``.

**Why not a live URL import.** Atlas-lang config is a sandbox: it may import only
``atlantide[.*]`` modules and cannot do network IO (see
:mod:`atlantide.lang.validate`), which keeps IR deterministic and byte-stable. So
fetching is a *separate, pinned* step — the ``terraform init`` model — and the
result is mounted under this package's namespace so the sandbox rules pass
**unchanged**: ``atlantide.components.<alias>`` already matches the allowed import
prefix, and the interpreter's ``importlib.import_module`` resolves it once
:func:`mount` has extended this package's ``__path__``.

**Trust.** A published component is third-party Python that runs unsandboxed (like a
provider). Integrity rests on pinning: the lock records the exact commit and a
content hash of the vendored tree, and ``atlantide component verify`` re-hashes to
detect tamper/drift. Vetting the code itself is the user's responsibility.

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
    ``__path__`` so that ``importlib.import_module("atlantide.components.<alias>")``
    — the interpreter's exact call for ``from atlantide.components.<alias> import
    ...`` — resolves the vendored subpackage. Idempotent, and a no-op when nothing
    has been vendored yet.

    Each locked alias is re-hashed against ``atlantide.lock`` first. Mounting is
    what makes third-party Python importable, so the pin is checked here rather
    than only in the opt-in ``atlantide component verify``; otherwise
    ``plan``/``apply``/``build`` execute a tampered or stale tree. Pass
    ``verify=False`` from the component commands, which rebuild that tree.
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

    An alias in the lock but absent from disk is the "not vendored yet" state and
    is skipped; the import itself reports it more clearly. The reverse — a
    directory on disk with no lock entry — is refused: :func:`mount` makes every
    directory under ``.atlantis/components`` importable, so an unlocked one would
    be third-party Python that runs with no hash verification at all.
    """
    # Imported inside the function: `components.fetch` imports this package's
    # verify helper, and a module-scope import here would close the loop.
    from atlantide.components.fetch import verify as verify_alias
    from atlantide.components.lock import LOCKFILE, load_lock
    from atlantide.core.errors import ComponentError

    root = components_dir(project_root)
    locked = load_lock(project_root)
    unlocked = sorted(
        p.name
        for p in (root.iterdir() if root.is_dir() else ())
        if p.is_dir() and p.name != "__pycache__" and p.name not in locked
    )
    if unlocked:
        names = ", ".join(repr(n) for n in unlocked)
        raise ComponentError(
            f"vendored component(s) {names} have no entry in {LOCKFILE}; "
            "remove the directory or re-run `atlantide component fetch` to pin it"
        )
    for alias, entry in locked.items():
        if (root / alias).is_dir():
            verify_alias(alias, entry, project_root)
