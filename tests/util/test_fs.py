"""`atlantide.util.fs`: owner-only files that never write through a symlink."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from atlantide.util.fs import (
    O_NOFOLLOW,
    OWNER_ONLY_MODE,
    create_private,
    find_upwards,
    write_private,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_constants() -> None:
    assert OWNER_ONLY_MODE == 0o600
    assert getattr(os, "O_NOFOLLOW", 0) == O_NOFOLLOW


# -- create_private ------------------------------------------------------------


@pytest.mark.parametrize("nofollow", [False, True])
def test_create_private_creates_an_empty_owner_only_file(tmp_path: Path, nofollow: bool) -> None:
    path = tmp_path / "state.db"
    assert create_private(path, nofollow=nofollow) is True
    assert path.read_bytes() == b""
    assert _mode(path) == 0o600


def test_create_private_accepts_a_str_path(tmp_path: Path) -> None:
    assert create_private(str(tmp_path / "state.db")) is True


def test_create_private_leaves_an_existing_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"keep")
    path.chmod(0o644)
    assert create_private(path) is False
    assert path.read_bytes() == b"keep"
    assert _mode(path) == 0o644


@pytest.mark.parametrize("nofollow", [False, True])
def test_create_private_does_not_create_through_a_dangling_symlink(
    tmp_path: Path, nofollow: bool
) -> None:
    target = tmp_path / "elsewhere"
    link = tmp_path / "state.db"
    link.symlink_to(target)
    assert create_private(link, nofollow=nofollow) is False
    assert not target.exists()


def test_create_private_propagates_other_os_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_private(tmp_path / "missing-dir" / "state.db")


# -- write_private -------------------------------------------------------------


@pytest.mark.parametrize("overwrite", [False, True])
def test_write_private_writes_a_new_owner_only_file(tmp_path: Path, overwrite: bool) -> None:
    path = tmp_path / "snap.atlas-state"
    write_private(path, b"payload", overwrite=overwrite)
    assert path.read_bytes() == b"payload"
    assert _mode(path) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_write_private_refuses_an_existing_file_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "snap"
    path.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        write_private(path, b"new", overwrite=False)
    assert path.read_bytes() == b"old"


def test_write_private_refuses_a_symlink_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_bytes(b"victim")
    link = tmp_path / "snap"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_private(link, b"new", overwrite=False)
    assert target.read_bytes() == b"victim"


def test_write_private_replaces_an_existing_file_with_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "snap"
    path.write_bytes(b"old")
    path.chmod(0o644)
    write_private(path, b"new", overwrite=True)
    assert path.read_bytes() == b"new"
    assert _mode(path) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap"]


def test_write_private_replaces_a_symlink_rather_than_writing_through_it(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_bytes(b"victim")
    link = tmp_path / "snap"
    link.symlink_to(target)
    write_private(link, b"new", overwrite=True)
    assert target.read_bytes() == b"victim"
    assert not link.is_symlink()
    assert link.read_bytes() == b"new"


def test_write_private_removes_the_temp_file_when_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "snap"
    path.write_bytes(b"old")

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        write_private(path, b"new", overwrite=True)
    assert path.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap"]


def test_write_private_propagates_a_write_failure_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        write_private(tmp_path / "snap", b"new", overwrite=False)


@pytest.mark.parametrize("overwrite", [False, True])
def test_write_private_propagates_an_open_failure(tmp_path: Path, overwrite: bool) -> None:
    with pytest.raises(FileNotFoundError):
        write_private(tmp_path / "missing-dir" / "snap", b"x", overwrite=overwrite)


# -- find_upwards --------------------------------------------------------------

_NAME = "util-find-upwards-marker.toml"


def test_find_upwards_finds_the_file_in_start(tmp_path: Path) -> None:
    (tmp_path / _NAME).write_text("")
    assert find_upwards(tmp_path, _NAME) == (tmp_path / _NAME).resolve()


def test_find_upwards_finds_the_nearest_ancestor(tmp_path: Path) -> None:
    (tmp_path / _NAME).write_text("")
    (tmp_path / "a" / _NAME).parent.mkdir()
    (tmp_path / "a" / _NAME).write_text("")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_upwards(deep, _NAME) == (tmp_path / "a" / _NAME).resolve()


def test_find_upwards_skips_a_directory_of_that_name(tmp_path: Path) -> None:
    (tmp_path / _NAME).write_text("")
    (tmp_path / "a" / _NAME).mkdir(parents=True)
    assert find_upwards(tmp_path / "a", _NAME) == (tmp_path / _NAME).resolve()


def test_find_upwards_resolves_a_relative_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / _NAME).write_text("")
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    assert find_upwards(Path("."), _NAME) == (tmp_path / _NAME).resolve()


def test_find_upwards_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_upwards(tmp_path, "no-such-file-anywhere-7f3c.toml") is None
