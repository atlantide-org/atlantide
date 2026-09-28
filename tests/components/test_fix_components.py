"""Regression tests for the components fix round: mount hardening, atomic vendoring,
git env isolation, reserved aliases, and a malformed lockfile."""

from __future__ import annotations

import pkgutil
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import atlantide.components as components
import atlantide.components.fetch as fetch_mod
from atlantide.components import components_dir
from atlantide.components._layout import PACKAGE_EXPORTS
from atlantide.components.fetch import fetch, tree_hash, vendor
from atlantide.components.lock import LockEntry, load_lock, write_lock
from atlantide.components.source import ComponentSource
from atlantide.core.errors import ComponentError

from .conftest import make_repo


@pytest.fixture
def clean_mount() -> Iterator[None]:
    saved = list(components.__path__)
    try:
        yield
    finally:
        components.__path__[:] = saved
        for name in list(sys.modules):
            if name.startswith("atlantide.components.") and name != "atlantide.components.lock":
                sys.modules.pop(name, None)


def _vendor_locked(project_root: Path, alias: str, body: str) -> Path:
    pkg = components_dir(project_root) / alias
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body)
    entries = dict(load_lock(project_root))
    entries[alias] = LockEntry(git="https://x/pkg", commit="c" * 40, hash=tree_hash(pkg))
    write_lock(project_root, entries)
    return pkg


# -- 1. non-directory entries in the components root -----------------------


@pytest.mark.parametrize("name", ["acme.py", "acme.pyc", "acme.cpython-312-darwin.so"])
def test_mount_refuses_module_file_replacing_locked_alias(
    tmp_path: Path, clean_mount: None, name: str
) -> None:
    # Lock `acme`, then swap its directory for a single-file module: it would be
    # importable as atlantide.components.acme without any hash check.
    pkg = _vendor_locked(tmp_path, "acme", "X = 1\n")
    shutil.rmtree(pkg)
    (components_dir(tmp_path) / name).write_text("X = 'pwned'\n")
    with pytest.raises(ComponentError, match=rf"{name!r}.*not a directory"):
        components.mount(tmp_path)
    assert str(components_dir(tmp_path)) not in components.__path__


def test_mount_refuses_symlinked_alias_dir(tmp_path: Path, clean_mount: None) -> None:
    pkg = _vendor_locked(tmp_path, "acme", "X = 1\n")
    real = tmp_path / "elsewhere"
    pkg.rename(real)
    pkg.symlink_to(real, target_is_directory=True)
    with pytest.raises(ComponentError, match="not a directory"):
        components.mount(tmp_path)


def test_mount_tolerates_unimportable_entries(tmp_path: Path, clean_mount: None) -> None:
    # Finder's .DS_Store, a .gitkeep, and names no import can reach are ignored.
    _vendor_locked(tmp_path, "acme", "X = 1\n")
    root = components_dir(tmp_path)
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (root / ".gitkeep").write_text("")
    (root / "not-a-module.py").write_text("X = 1\n")
    (root / "class.py").write_text("X = 1\n")  # keyword: never importable
    (root / "some.dir").mkdir()  # a dir matches on its whole name
    components.mount(tmp_path)
    assert str(root) in components.__path__


@pytest.mark.parametrize("name", ["stray.txt", "__pycache__"])
def test_mount_refuses_other_files_with_importable_names(
    tmp_path: Path, clean_mount: None, name: str
) -> None:
    _vendor_locked(tmp_path, "acme", "X = 1\n")
    (components_dir(tmp_path) / name).write_text("")
    with pytest.raises(ComponentError, match="not a directory"):
        components.mount(tmp_path)


def test_mount_allows_root_pycache(tmp_path: Path, clean_mount: None) -> None:
    _vendor_locked(tmp_path, "acme", "X = 1\n")
    (components_dir(tmp_path) / "__pycache__").mkdir()
    components.mount(tmp_path)


# -- 2. stale bytecode is removed before mounting --------------------------


def test_mount_purges_bytecode_under_vendored_alias(tmp_path: Path, clean_mount: None) -> None:
    pkg = _vendor_locked(tmp_path, "acme", "X = 1\n")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"forged")
    (pkg / "sub").mkdir()
    (pkg / "sub" / "mod.pyc").write_bytes(b"forged")
    components.mount(tmp_path)  # hash excludes bytecode, so this still verifies
    assert not cache.exists()
    assert not (pkg / "sub" / "mod.pyc").exists()


def test_forged_pyc_is_not_loaded(tmp_path: Path, clean_mount: None) -> None:
    # A .pyc whose header matches the source's mtime/size would be loaded instead
    # of the source; mount must delete it so the verified source is what runs.
    import importlib
    import py_compile

    pkg = _vendor_locked(tmp_path, "acme", "X = 1\n")
    init = pkg / "__init__.py"
    stat = init.stat()
    forged_src = tmp_path / "forged.py"
    forged_src.write_text("X = 2\n")  # same size as the real source
    import importlib.util

    cached = Path(importlib.util.cache_from_source(str(init)))
    cached.parent.mkdir(exist_ok=True)
    py_compile.compile(str(forged_src), cfile=str(cached), doraise=True)
    # Rewrite the header's mtime/size fields to match the vendored source.
    data = bytearray(cached.read_bytes())
    data[8:12] = int(stat.st_mtime).to_bytes(4, "little")
    data[12:16] = (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little")
    cached.write_bytes(bytes(data))

    components.mount(tmp_path)
    assert importlib.import_module("atlantide.components.acme").X == 1


# -- 3. a failing vendor/fetch leaves the previous tree intact --------------


def test_failed_vendor_keeps_previous_tree(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    project = tmp_path / "project"
    good = fetch("acme", ComponentSource(git=url, ref="v1", subdir="pkg"), project)
    vendored = components_dir(project) / "acme" / "__init__.py"

    wrong = LockEntry(git=good.git, commit=good.commit, hash="sha256.v2:" + "0" * 64, subdir="pkg")
    with pytest.raises(ComponentError, match="lock pins"):
        vendor("acme", wrong, project)
    assert vendored.read_text() == "VALUE = 1\n"
    # No staging leftovers anywhere under the components root.
    assert sorted(p.name for p in components_dir(project).iterdir()) == ["acme"]


def test_rejected_fetch_keeps_previous_tree(tmp_path: Path) -> None:
    good_src = tmp_path / "good"
    make_repo(good_src)
    project = tmp_path / "project"
    fetch("acme", ComponentSource(git=f"file://{good_src}", ref="v1", subdir="pkg"), project)

    bad_src = tmp_path / "bad"
    (bad_src / "pkg").mkdir(parents=True)
    (bad_src / "pkg" / "leak").symlink_to("/etc/passwd")
    make_repo(bad_src)
    with pytest.raises(ComponentError, match="symlink"):
        fetch("acme", ComponentSource(git=f"file://{bad_src}", ref="v1", subdir="pkg"), project)
    assert (components_dir(project) / "acme" / "__init__.py").read_text() == "VALUE = 1\n"


def test_copy_error_keeps_previous_tree(
    repo: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = repo
    project = tmp_path / "project"
    entry = fetch("acme", ComponentSource(git=url, ref="v1", subdir="pkg"), project)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fetch_mod.shutil, "copytree", boom)
    with pytest.raises(OSError, match="disk full"):
        vendor("acme", entry, project)
    assert (components_dir(project) / "acme" / "__init__.py").read_text() == "VALUE = 1\n"


def test_vendor_replaces_existing_tree(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    project = tmp_path / "project"
    entry = fetch("acme", ComponentSource(git=url, ref="v1", subdir="pkg"), project)
    stray = components_dir(project) / "acme" / "stray.py"
    stray.write_text("junk\n")
    vendor("acme", entry, project)
    assert not stray.exists()


# -- 4. repo-local git env vars do not leak into git calls ------------------


def test_git_env_drops_repo_local_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in fetch_mod._GIT_LOCAL_ENV_VARS:
        monkeypatch.setenv(var, "/hijack")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    monkeypatch.setenv("GIT_ASKPASS", "/bin/true")
    env = fetch_mod._git_env()
    assert not set(fetch_mod._GIT_LOCAL_ENV_VARS) & set(env)
    assert env["GIT_SSH_COMMAND"] == "ssh -i key"
    assert env["GIT_ASKPASS"] == "/bin/true"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_local_env_vars_cover_the_documented_set() -> None:
    # `git rev-parse --local-env-vars` as of git 2.45.
    expected = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
        "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
    }  # fmt: skip
    assert expected <= set(fetch_mod._GIT_LOCAL_ENV_VARS)


def test_fetch_ignores_callers_git_dir(
    repo: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, commit = repo
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    entry = fetch("acme", ComponentSource(git=url, ref="v1", subdir="pkg"), tmp_path / "p")
    assert entry.commit == commit


# -- 5. aliases naming the package's own modules are reserved ---------------


def test_reserved_aliases_match_package_submodules() -> None:
    pkg_dir = Path(components.__file__).parent
    submodules = {m.name for m in pkgutil.iter_modules([str(pkg_dir)])}
    assert {"fetch", "lock", "source"} <= submodules
    assert submodules <= fetch_mod._RESERVED_ALIASES


def test_reserved_aliases_cover_the_package_exports() -> None:
    # `fetch` reads the export list from `_layout`, not from the package it would
    # otherwise import back; the two lists must not drift apart.
    assert set(components.__all__) == PACKAGE_EXPORTS
    assert PACKAGE_EXPORTS <= fetch_mod._RESERVED_ALIASES


@pytest.mark.parametrize("alias", ["lock", "fetch", "source", "mount"])
def test_alias_colliding_with_package_is_rejected(
    alias: str, repo: tuple[str, str], tmp_path: Path
) -> None:
    url, _ = repo
    with pytest.raises(ComponentError, match="reserved"):
        fetch(alias, ComponentSource(git=url, ref="v1", subdir="pkg"), tmp_path)
    assert not (components_dir(tmp_path) / alias).exists()


# -- 6. a malformed lockfile is a ComponentError ----------------------------


@pytest.mark.parametrize("raw", [b"[components.acme\ngit = ", b"\xff\xfe not utf-8"])
def test_malformed_lockfile_raises_component_error(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / "atlantide.lock").write_bytes(raw)
    with pytest.raises(ComponentError, match=r"atlantide\.lock"):
        load_lock(tmp_path)
