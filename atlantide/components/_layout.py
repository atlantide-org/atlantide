"""Where vendored components live on disk.

A leaf module so `components.fetch` can import the layout without importing the
package ``__init__`` that imports *it* — the cycle the function-local imports in
``__init__`` used to work around.
"""

from __future__ import annotations

from pathlib import Path

#: Hidden project dir holding vendored component trees (derived; not committed).
VENDOR_DIR = ".atlantis"
_COMPONENTS_SUBDIR = "components"


def components_dir(project_root: Path) -> Path:
    """The dir under which each alias's vendored package tree lives."""
    return project_root / VENDOR_DIR / _COMPONENTS_SUBDIR
