"""Emitting ``atlantide.lock`` safely.

A git url or ``--ref`` is attacker-influenced text that ends up inside a TOML
string, so an unescaped quote or newline closes the value early and injects
whatever follows — a ``[state]`` table, for one.
"""

from __future__ import annotations

from pathlib import Path

from atlantide.components.lock import LockEntry, load_lock, toml_string, write_lock


def test_lock_values_are_escaped(tmp_path: Path) -> None:
    """A url or --ref carrying a quote or newline would close the string early and
    inject arbitrary TOML, such as a [state] table."""
    hostile = 'https://x/y"\n[state]\nbackend = "s3'
    write_lock(tmp_path, {"acme": LockEntry(git=hostile, commit="c" * 40, hash="sha256:x")})

    text = (tmp_path / "atlantide.lock").read_text()
    assert "\n[state]" not in text
    assert load_lock(tmp_path)["acme"].git == hostile  # and it round-trips


def test_toml_string_escapes_backslashes_first() -> None:
    assert toml_string(r"a\b") == r'"a\\b"'
    assert toml_string('a"b') == r'"a\"b"'
