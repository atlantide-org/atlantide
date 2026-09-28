"""Where vendored components live on disk, and the package's own names.

A leaf module: both the package ``__init__`` and ``components.fetch`` need the
layout, and ``__init__`` imports ``fetch``, so the layout imports neither.
"""

from __future__ import annotations

from pathlib import Path

#: Hidden project dir holding vendored component trees (derived; not committed).
VENDOR_DIR = ".atlantis"
_COMPONENTS_SUBDIR = "components"

#: The package ``__all__``, which ``fetch`` reserves as aliases: importing
#: ``atlantide.components.<alias>`` would overwrite the package attribute. Kept
#: here so ``fetch`` need not import the package that lazily imports it; a test
#: holds it equal to ``atlantide.components.__all__``.
PACKAGE_EXPORTS = frozenset({"VENDOR_DIR", "components_dir", "mount", "verify_vendored"})


def components_dir(project_root: Path) -> Path:
    """The dir under which each alias's vendored package tree lives."""
    return project_root / VENDOR_DIR / _COMPONENTS_SUBDIR
