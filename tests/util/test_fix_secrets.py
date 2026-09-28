"""Regression tests: `write_private` cleans up after a failed write."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlantide.util import fs
from atlantide.util.fs import fsync_dir, write_private


@pytest.mark.parametrize("error", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_create_removes_the_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """A truncated file must not be left behind to trip "already exists" on retry."""
    path = tmp_path / "snap"

    def fail(fd: int) -> None:
        raise error

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(type(error)):
        write_private(path, b"new", overwrite=False)
    assert not path.exists()
    monkeypatch.undo()
    write_private(path, b"new", overwrite=False)  # the retry succeeds
    assert path.read_bytes() == b"new"


def test_failed_create_never_removes_a_preexisting_file(tmp_path: Path) -> None:
    path = tmp_path / "snap"
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        write_private(path, b"new", overwrite=False)
    assert path.read_bytes() == b"keep"


def test_interrupted_overwrite_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "snap"
    path.write_bytes(b"old")

    def interrupt(src: object, dst: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        write_private(path, b"new", overwrite=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap"]
    assert path.read_bytes() == b"old"


def test_overwrite_fsyncs_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(fs, "fsync_dir", synced.append)
    write_private(tmp_path / "snap", b"new", overwrite=True)
    assert synced == [tmp_path]


def test_fsync_dir_is_best_effort(tmp_path: Path) -> None:
    fsync_dir(tmp_path)
    fsync_dir(tmp_path / "missing")  # swallowed, not raised
