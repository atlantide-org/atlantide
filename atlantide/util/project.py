"""Project file lookup.

The CLI reads it for defaults and the local provider for its path root; both
must find the same file, and ``providers`` may not import the CLI.
"""

from __future__ import annotations

from pathlib import Path

from atlantide.util.fs import find_upwards

__all__ = ["PROJECT_FILENAME", "find_project_file"]

#: Project file name, searched for upward from the cwd; ``atlantide init`` writes it.
PROJECT_FILENAME = "atlantide.toml"


def find_project_file(start: Path | None = None) -> Path | None:
    """``atlantide.toml`` in ``start`` (cwd by default) or the nearest ancestor holding one."""
    return find_upwards(start or Path.cwd(), PROJECT_FILENAME)
