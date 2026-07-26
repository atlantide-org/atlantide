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
    assert entry.hash.startswith("sha256.v2:")
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


# -- untrusted inputs -------------------------------------------------------
#
# The alias, url, ref, and subdir all come from `atlantide.toml` / `atlantide.lock`
# — files that arrive with a cloned repo. `_materialize` rmtree's the destination
# and shells out to git, so none of the four may be taken at face value.

#: Aliases that escape `.atlantis/components` by lexical path joining. `_dest`
#: feeds the result straight to `shutil.rmtree`.
TRAVERSING_ALIASES = ["../../../../tmp/victim", "..", "a/b", "/etc", ""]


@pytest.mark.parametrize("alias", TRAVERSING_ALIASES)
def test_alias_outside_the_project_is_rejected(
    alias: str, repo: tuple[str, str], tmp_path: Path
) -> None:
    url, _ = repo
    with pytest.raises(ComponentError, match="alias"):
        fetch(alias, _source(url), tmp_path)


def test_alias_traversal_does_not_delete_the_target(tmp_path: Path) -> None:
    """The check must precede `rmtree`, not merely occur somewhere in `fetch`."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("important\n")

    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ComponentError):
        fetch("../../victim", _source("file:///nonexistent"), project)
    assert (victim / "keep.txt").read_text() == "important\n"


#: `ext::<cmd>` is a git remote helper: git runs the string as a shell command.
#: A leading `-` makes git parse the "url" as an option (`--upload-pack=<cmd>`).
HOSTILE_URLS = [
    "ext::sh -c 'touch /tmp/pwned'",
    "--upload-pack=/bin/sh",
    "-c protocol.ext.allow=always",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_hostile_url_is_rejected(url: str, tmp_path: Path) -> None:
    with pytest.raises(ComponentError, match="url"):
        fetch("acme", ComponentSource(git=url), tmp_path)


def test_hostile_ref_is_rejected(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    with pytest.raises(ComponentError, match="ref"):
        fetch("acme", ComponentSource(git=url, ref="--upload-pack=/bin/sh"), tmp_path)


def test_subdir_cannot_escape_the_clone(repo: tuple[str, str], tmp_path: Path) -> None:
    url, _ = repo
    with pytest.raises(ComponentError, match="outside the component repo"):
        fetch("acme", _source(url, subdir="../../../../etc"), tmp_path)


def test_tree_hash_framing_is_injective(tmp_path: Path) -> None:
    # The pre-v2 `path\0content\0` concatenation collided for these two trees
    # (embedded NULs let one tree impersonate another); length framing must not.
    one = tmp_path / "one"
    one.mkdir()
    (one / "a").write_bytes(b"1\x00b\x002")
    two = tmp_path / "two"
    two.mkdir()
    (two / "a").write_bytes(b"1")
    (two / "b").write_bytes(b"2")
    assert tree_hash(one) != tree_hash(two)


def test_verify_rejects_outdated_lock_hash_format(repo: tuple[str, str], tmp_path: Path) -> None:
    url, commit = repo
    fetch("acme", _source(url), tmp_path)
    stale = LockEntry(git=url, commit=commit, hash="sha256:" + "0" * 64, subdir="pkg")
    with pytest.raises(ComponentError, match="outdated format"):
        verify("acme", stale, tmp_path)
