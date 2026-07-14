"""The declared git source of a published component.

Where a :class:`~atlantide.components.lock.LockEntry` is the *resolved* pin (exact
commit + hash), a :class:`ComponentSource` is the *request*: the repo, the ref to
fetch, and where the package sits inside it. It is parsed from the
``[components.<alias>]`` table in ``atlantide.toml`` (see
:func:`atlantide.cli.project.load_project`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentSource:
    """A published component's git source, as declared in ``atlantide.toml``.

    The requested ``ref`` (tag/branch/commit) is resolved to an exact commit and
    content hash at lock time.
    """

    git: str
    ref: str | None = None
    subdir: str | None = None
