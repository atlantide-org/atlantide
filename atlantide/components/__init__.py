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

#: Hidden project dir holding vendored component trees (derived; not committed).
VENDOR_DIR = ".atlantis"
_COMPONENTS_SUBDIR = "components"


def components_dir(project_root: Path) -> Path:
    """The dir under which each alias's vendored package tree lives."""
    return project_root / VENDOR_DIR / _COMPONENTS_SUBDIR


def mount(project_root: Path) -> None:
    """Make vendored components importable as ``atlantide.components.<alias>``.

    Appends the project's ``.atlantis/components`` dir to this package's
    ``__path__`` so that ``importlib.import_module("atlantide.components.<alias>")``
    — the interpreter's exact call for ``from atlantide.components.<alias> import
    ...`` — resolves the vendored subpackage. Idempotent, and a no-op when nothing
    has been vendored yet.
    """
    root = components_dir(project_root)
    entry = str(root)
    if root.is_dir() and entry not in __path__:
        __path__.append(entry)
