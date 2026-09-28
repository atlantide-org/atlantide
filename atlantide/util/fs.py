"""Owner-only file creation and upward file lookup.

State databases, snapshots, keyfiles and secrets stores hold identifiers,
outputs and key material, so they are created ``0600`` rather than under the
process umask (usually world-readable).
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from pathlib import Path

__all__ = [
    "OWNER_ONLY_MODE",
    "O_NOFOLLOW",
    "create_private",
    "find_upwards",
    "fsync_dir",
    "write_private",
]

#: Mode for every file holding state, key material or ciphertext.
OWNER_ONLY_MODE = 0o600

#: Refuse to follow a symlink at the final path component, where supported.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)


def create_private(path: str | os.PathLike[str], *, nofollow: bool = False) -> bool:
    """Create ``path`` empty and owner-only; ``False`` if it already exists.

    An existing file is left alone: its mode is the operator's choice. sqlite
    gives its ``-wal``/``-shm`` files the main file's mode, so pre-creating a
    database this way keeps those private too. Any other :class:`OSError`
    propagates for the caller to word.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | (O_NOFOLLOW if nofollow else 0)
    try:
        fd = os.open(path, flags, OWNER_ONLY_MODE)
    except FileExistsError:
        return False  # possibly created by a concurrent opener
    os.close(fd)
    return True


def write_private(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write ``data`` to ``path`` owner-only, never through a planted symlink.

    Without ``overwrite`` the file is created with ``O_EXCL``, so a path that
    appears after the caller's check, or is a symlink, raises
    :class:`FileExistsError`. With ``overwrite`` the bytes go to a fresh temp file
    beside ``path`` that is renamed over it (then the directory is fsynced), so
    readers never see a partial file and a symlink is replaced rather than
    written through. If the write fails or is interrupted, the file this call
    created (the temp file, or the new file itself) is removed, so no truncated
    file is left to block a retry.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW
    target = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp") if overwrite else path
    fd = os.open(target, flags, OWNER_ONLY_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if overwrite:
            os.replace(target, path)
    except BaseException:
        # O_EXCL succeeded, so ``target`` is ours to remove.
        target.unlink(missing_ok=True)
        raise
    if overwrite:
        fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    """Best-effort fsync of directory ``path``, making a link/rename in it durable."""
    with suppress(OSError):
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def find_upwards(start: Path, filename: str) -> Path | None:
    """``filename`` in ``start`` or the nearest ancestor holding one, as git does."""
    directory = start.resolve()
    for candidate in (directory, *directory.parents):
        path = candidate / filename
        if path.is_file():
            return path
    return None
