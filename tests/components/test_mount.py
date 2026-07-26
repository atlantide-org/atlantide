"""``mount`` makes a vendored tree importable as ``atlantide.components.<alias>``."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import atlantide.components as components
from atlantide.core.errors import ComponentError


@pytest.fixture
def clean_mount() -> Iterator[None]:
    """Snapshot/restore the package ``__path__`` and drop any imported subpackage,
    so mounting in one test never leaks into another."""
    saved = list(components.__path__)
    try:
        yield
    finally:
        components.__path__[:] = saved
        for name in list(sys.modules):
            if name.startswith("atlantide.components.") and name != "atlantide.components.lock":
                sys.modules.pop(name, None)


def _vendor(project_root: Path, alias: str, body: str, *, lock: bool = True) -> None:
    from atlantide.components.fetch import tree_hash
    from atlantide.components.lock import LockEntry, load_lock, write_lock

    pkg = components.components_dir(project_root) / alias
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body)
    if lock:
        entries = dict(load_lock(project_root))
        entries[alias] = LockEntry(git="https://x/pkg", commit="c" * 40, hash=tree_hash(pkg))
        write_lock(project_root, entries)


def test_mount_makes_vendored_pkg_importable(tmp_path: Path, clean_mount: None) -> None:
    _vendor(tmp_path, "acme", "HELLO = 'world'\n")
    components.mount(tmp_path)
    module = importlib.import_module("atlantide.components.acme")
    assert module.HELLO == "world"


def test_mount_is_idempotent(tmp_path: Path, clean_mount: None) -> None:
    _vendor(tmp_path, "acme", "X = 1\n")
    components.mount(tmp_path)
    components.mount(tmp_path)
    entry = str(components.components_dir(tmp_path))
    assert components.__path__.count(entry) == 1


def test_mount_noop_when_nothing_vendored(tmp_path: Path, clean_mount: None) -> None:
    before = list(components.__path__)
    components.mount(tmp_path)  # no .atlantis dir exists
    assert components.__path__ == before


def test_mount_refuses_unlocked_vendored_dir(tmp_path: Path, clean_mount: None) -> None:
    # A directory under .atlantis/components with no lock entry would be
    # importable third-party code with no hash verification; mount must refuse.
    _vendor(tmp_path, "acme", "X = 1\n", lock=False)
    with pytest.raises(ComponentError, match=r"'acme'.*no entry"):
        components.mount(tmp_path)
