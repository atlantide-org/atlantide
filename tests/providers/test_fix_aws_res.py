"""Regression tests: random-length validation, local File writes, and the
``ATLANTIDE_NO_PLUGINS`` switch."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from atlantide.core import Context
from atlantide.providers import loader
from atlantide.providers.loader import NO_PLUGINS_ENV, discover
from atlantide.providers.local import File, LocalProvider, SourceFile
from atlantide.providers.random import Id, Password
from tests.support.fakeplugin import PLUGIN as ACME

# -- item 6: random lengths must be positive --------------------------------------


@pytest.mark.parametrize("length", [0, -1])
def test_password_length_must_be_positive(length: int) -> None:
    with pytest.raises(ValueError, match="length"):
        Password("p", length=length)


@pytest.mark.parametrize("length", [0, -1])
def test_id_byte_length_must_be_positive(length: int) -> None:
    with pytest.raises(ValueError, match="byte_length"):
        Id("i", byte_length=length)


def test_minimal_lengths_pass() -> None:
    Password("p", length=1)
    Id("i", byte_length=1)


# -- item 7: File writes are UTF-8, untranslated, atomic ---------------------------


def _sum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_file_is_written_as_utf8_without_newline_translation(tmp_path: Path) -> None:
    content = "café\r\nline\n"
    target = tmp_path / "f.txt"
    out = await LocalProvider().create(Context(), File("f", path=str(target), content=content))
    raw = content.encode("utf-8")
    assert target.read_bytes() == raw
    assert out["checksum"] == _sum(raw)


async def test_read_sees_a_crlf_change(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "f.txt"
    res = File("f", path=str(target), content="a\nb\n")
    created = await provider.create(Context(), res)
    target.write_bytes(b"a\r\nb\r\n")  # an editor converted the line endings
    observed = await provider.read(Context(), res)
    assert observed is not None
    assert observed["checksum"] != created["checksum"]


async def test_update_creates_a_missing_parent(tmp_path: Path) -> None:
    target = tmp_path / "gone" / "f.txt"
    res = File("f", path=str(target), content="v2")
    await LocalProvider().update(Context(), {}, res)
    assert target.read_text(encoding="utf-8") == "v2"


async def test_write_leaves_no_temp_file_and_keeps_the_mode(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "d" / "f.sh"
    await provider.create(Context(), File("f", path=str(target), content="v1"))
    if os.name != "nt":
        target.chmod(0o750)
    await provider.update(Context(), {}, File("f", path=str(target), content="v2"))
    assert target.read_text(encoding="utf-8") == "v2"
    assert sorted(p.name for p in target.parent.iterdir()) == ["f.sh"]
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o750


async def test_source_file_checksum_matches_the_bytes_read(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_bytes("é\r\n".encode())
    res = SourceFile("s", path=str(source))
    assert res.checksum == _sum(source.read_bytes())
    out = await LocalProvider().read(Context(), res)
    assert out == {"content": "é\r\n"}


# -- item 9: ATLANTIDE_NO_PLUGINS accepts only true values -------------------------


@pytest.mark.parametrize(
    ("value", "disabled"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
    ],
)
def test_no_plugins_env_is_parsed_as_a_boolean(
    monkeypatch: pytest.MonkeyPatch, value: str, disabled: bool
) -> None:
    monkeypatch.setattr(loader, "entry_points", lambda group: [_Entry()])
    monkeypatch.setenv(NO_PLUGINS_ENV, value)
    names = [p.name for p in discover().plugins]
    assert ("acme" not in names) is disabled


class _Entry:
    name = "acme"
    value = "tests.support.fakeplugin:PLUGIN"

    def load(self) -> object:
        return ACME
