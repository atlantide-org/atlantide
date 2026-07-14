"""Fetch/vendor/verify + tree hashing against a local git repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.components import components_dir
from atlantide.components.fetch import fetch, tree_hash, vendor, verify
from atlantide.components.lock import LockEntry
from atlantide.components.source import ComponentSource
from atlantide.core.errors import ComponentError

from .conftest import make_repo


def _source(url: str, subdir: str = "pkg") -> ComponentSource:
    return ComponentSource(git=url, ref="v1", subdir=subdir)


def test_tree_hash_is_deterministic_and_ignores_pycache(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n")
    (root / "sub" / "b.py").write_text("y = 2\n")
    baseline = tree_hash(root)

    # Rehashing is stable; a derived cache file must not move the hash.
    assert tree_hash(root) == baseline
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_bytes(b"\x00\x01")
    (root / "sub" / "b.pyc").write_bytes(b"\x00\x02")
    assert tree_hash(root) == baseline

    # A real content change does move it.
    (root / "a.py").write_text("x = 2\n")
    assert tree_hash(root) != baseline


def test_fetch_vendors_subdir_and_pins_commit(repo: tuple[str, str], tmp_path: Path) -> None:
    url, commit = repo
    entry = fetch("acme", _source(url), tmp_path)

    assert entry.commit == commit
    assert entry.subdir == "pkg"
    assert entry.hash.startswith("sha256:")
    vendored = components_dir(tmp_path) / "acme"
    assert (vendored / "__init__.py").read_text() == "VALUE = 1\n"
    assert entry.hash == tree_hash(vendored)


def test_verify_passes_clean_then_fails_on_tamper(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    entry = fetch("acme", _source(url), tmp_path)
    verify("acme", entry, tmp_path)  # clean: no raise

    (components_dir(tmp_path) / "acme" / "__init__.py").write_text("VALUE = 999\n")
    with pytest.raises(ComponentError, match="tampered or drifted"):
        verify("acme", entry, tmp_path)


def test_vendor_rematerializes_from_lock_alone(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    entry = fetch("acme", _source(url), tmp_path)
    tampered = components_dir(tmp_path) / "acme" / "__init__.py"
    tampered.write_text("broken\n")

    vendor("acme", entry, tmp_path)  # restores from the lock entry + asserts hash
    assert tampered.read_text() == "VALUE = 1\n"
    verify("acme", entry, tmp_path)


def test_missing_subdir_errors(tmp_path: Path) -> None:
    src = tmp_path / "repo"
    make_repo(src)  # package is at pkg/, not at src/
    with pytest.raises(ComponentError, match="subdir 'nope' not found"):
        fetch("acme", _source(f"file://{src}", subdir="nope"), tmp_path)


def test_verify_unvendored_errors(tmp_path: Path) -> None:
    entry = LockEntry(git="x", commit="c", hash="sha256:whatever")
    with pytest.raises(ComponentError, match="not vendored"):
        verify("ghost", entry, tmp_path)
