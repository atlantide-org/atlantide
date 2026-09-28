"""Resolution and confinement of local paths.

Relative paths resolve against the **project root** (the directory holding
``atlantide.toml``), not the working directory, so that commands run from
different directories address the same file.

Resolved paths (symlinks followed) must stay inside that root, so that a config
and the components it imports cannot write, delete, or read (via ``SourceFile``)
arbitrary files. A project opts out with::

    [provider.local]
    allow_outside_project = true

Without an ``atlantide.toml``, the working directory at scope construction is
the root and confines the same way. ``allow_outside_project=True`` (a
``LocalProvider`` keyword or the provider settings key) opts out.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from atlantide.util.project import PROJECT_FILENAME as PROJECT_FILENAME
from atlantide.util.project import find_project_file

#: The ``[provider.local]`` key that lifts confinement.
ALLOW_OUTSIDE_KEY = "allow_outside_project"


class PathEscapeError(ValueError):
    """A local path resolves outside the project root."""


@dataclass(frozen=True, slots=True)
class PathScope:
    """How local paths resolve: against ``root``, confined to it unless opted out.

    ``root`` of ``None`` means no project: the working directory at construction
    becomes the root, and ``implicit_root`` records that for the error message. It
    is captured once so a later ``chdir`` does not move the boundary.
    """

    root: Path | None = None
    allow_outside_project: bool = False
    implicit_root: bool = False

    def __post_init__(self) -> None:
        if self.root is None:
            object.__setattr__(self, "root", Path.cwd())
            object.__setattr__(self, "implicit_root", True)

    @property
    def confined(self) -> bool:
        return not self.allow_outside_project

    def resolve(self, path: str) -> Path:
        """Resolve ``path`` against the root, following symlinks and checking for escape."""
        assert self.root is not None  # set by __post_init__
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if self.confined:
            root = self.root.resolve()
            if not resolved.is_relative_to(root):
                raise PathEscapeError(self._escape_message(path, resolved, root))
        return resolved

    def _escape_message(self, path: str, resolved: Path, root: Path) -> str:
        if self.implicit_root:
            return (
                f"local path {path!r} resolves to {resolved}, outside the working "
                f"directory {root} (no {PROJECT_FILENAME} found, so it stands in as the "
                f"project root); pass `{ALLOW_OUTSIDE_KEY}=True` to LocalProvider, or set "
                f"`{ALLOW_OUTSIDE_KEY} = true` under [provider.local] in a "
                f"{PROJECT_FILENAME}, to allow it"
            )
        return (
            f"local path {path!r} resolves to {resolved}, outside the project "
            f"root {root}; set `{ALLOW_OUTSIDE_KEY} = true` under "
            f"[provider.local] in {PROJECT_FILENAME} to allow it"
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Self:
        """Build the scope described by the provider's settings table.

        ``root`` is the project root set by the CLI, absent without a project file.
        ``allow_outside_project`` comes from ``[provider.local]``.
        """
        root = settings.get("root")
        return cls(
            root=Path(root) if root else None,
            allow_outside_project=settings.get(ALLOW_OUTSIDE_KEY) is True,
        )

    @classmethod
    def discover(cls, start: Path | None = None) -> Self:
        """Build the scope for the project enclosing ``start`` (cwd by default).

        Used where provider settings are unavailable, such as ``SourceFile``
        fingerprinting during config evaluation. Locates the project the same way
        the CLI does (nearest ``atlantide.toml`` at or above ``start``), so both
        agree on the root. Without a project file, ``start`` is the root.
        """
        directory = (start or Path.cwd()).resolve()
        project_file = find_project_file(directory)
        if project_file is None:
            return cls(root=directory, implicit_root=True)
        return cls(root=project_file.parent, allow_outside_project=_allows_outside(project_file))


def _allows_outside(project_file: Path) -> bool:
    """Return whether ``[provider.local]`` in ``project_file`` opts out of confinement.

    An unreadable file fails closed (confined); the CLI reports the parse error.
    """
    try:
        with project_file.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    providers = data.get("provider")
    table = providers.get("local") if isinstance(providers, dict) else None
    return isinstance(table, dict) and table.get(ALLOW_OUTSIDE_KEY) is True
