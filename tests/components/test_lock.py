"""``atlantide.lock`` read/write roundtrip."""

from __future__ import annotations

from pathlib import Path

from atlantide.components.lock import LockEntry, load_lock, write_lock


def test_missing_lock_is_empty(tmp_path: Path) -> None:
    assert load_lock(tmp_path) == {}


def test_roundtrip(tmp_path: Path) -> None:
    entries = {
        "acme": LockEntry(git="https://x/acme", commit="a" * 40, hash="sha256:aa", subdir="src"),
        "beta": LockEntry(git="https://x/beta", commit="b" * 40, hash="sha256:bb"),  # no subdir
    }
    write_lock(tmp_path, entries)
    assert load_lock(tmp_path) == entries


def test_written_lock_is_alias_sorted(tmp_path: Path) -> None:
    write_lock(
        tmp_path,
        {
            "zeta": LockEntry(git="g", commit="c", hash="h"),
            "alpha": LockEntry(git="g", commit="c", hash="h"),
        },
    )
    text = (tmp_path / "atlantide.lock").read_text()
    assert text.index("[components.alpha]") < text.index("[components.zeta]")


def test_malformed_entry_skipped(tmp_path: Path) -> None:
    (tmp_path / "atlantide.lock").write_text(
        '[components.ok]\ngit = "g"\ncommit = "c"\nhash = "h"\n'
        "[components.bad]\ngit = 5\n"  # non-string git -> skipped
    )
    assert set(load_lock(tmp_path)) == {"ok"}
