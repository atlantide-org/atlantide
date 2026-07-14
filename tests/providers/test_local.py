"""LocalProvider: real disk CRUD for File, no-ops for Null."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atlantide.core import Context
from atlantide.core.errors import LanguageError, ProviderError
from atlantide.providers.local import File, LocalProvider, Null, SourceFile


def _sum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def test_file_create_read_update_delete(tmp_path: Path) -> None:
    provider = LocalProvider()
    ctx = Context()
    target = tmp_path / "sub" / "hello.txt"
    res = File("hello", path=str(target), content="hi")

    out = await provider.create(ctx, res)
    assert target.read_text() == "hi"
    assert out == {"checksum": _sum("hi"), "path": str(target)}

    assert await provider.read(ctx, res) == {"checksum": _sum("hi"), "path": str(target)}

    updated = File("hello", path=str(target), content="bye")
    out2 = await provider.update(ctx, out, updated)
    assert target.read_text() == "bye"
    assert out2["checksum"] == _sum("bye")

    await provider.delete(ctx, updated)
    assert not target.exists()


async def test_read_missing_file_is_none(tmp_path: Path) -> None:
    provider = LocalProvider()
    res = File("x", path=str(tmp_path / "nope.txt"))
    assert await provider.read(Context(), res) is None


async def test_delete_missing_is_noop(tmp_path: Path) -> None:
    provider = LocalProvider()
    res = File("x", path=str(tmp_path / "nope.txt"))
    await provider.delete(Context(), res)  # no error


async def test_create_wraps_os_error_with_resource_context(tmp_path: Path) -> None:
    # A regular file blocks its use as a parent directory -> raw OSError on write,
    # which the provider wraps into a ProviderError tagged with op + resource type.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    res = File("child", path=str(blocker / "child.txt"), content="hi")

    with pytest.raises(ProviderError) as ei:
        await LocalProvider().create(Context(), res)

    err = ei.value
    assert err.op == "create"
    assert err.resource_type == res.type_name()
    assert isinstance(err.__cause__, OSError)  # original error preserved


async def test_null_resource_is_noop() -> None:
    provider = LocalProvider()
    res = Null("n", triggers={"k": "v"})
    assert await provider.create(Context(), res) == {}
    assert await provider.delete(Context(), res) is None


async def test_sourcefile_reads_content_and_fingerprints(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "data.txt"
    target.write_text("hello")
    res = SourceFile("s", path=str(target))

    assert res.checksum == _sum("hello")  # fingerprint read at construction
    assert await provider.create(Context(), res) == {"content": "hello"}
    assert await provider.read(Context(), res) == {"content": "hello"}


async def test_sourcefile_read_reflects_live_change(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "data.txt"
    target.write_text("v1")
    res = SourceFile("s", path=str(target))
    assert await provider.create(Context(), res) == {"content": "v1"}

    target.write_text("v2")  # mutate on disk
    assert await provider.read(Context(), res) == {"content": "v2"}
    assert await provider.update(Context(), {"content": "v1"}, res) == {"content": "v2"}


async def test_sourcefile_read_missing_is_none(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "data.txt"
    target.write_text("x")
    res = SourceFile("s", path=str(target))
    target.unlink()
    assert await provider.read(Context(), res) is None


async def test_sourcefile_delete_is_noop_and_keeps_file(tmp_path: Path) -> None:
    provider = LocalProvider()
    target = tmp_path / "data.txt"
    target.write_text("keep")
    res = SourceFile("s", path=str(target))
    await provider.delete(Context(), res)
    assert target.read_text() == "keep"


def test_sourcefile_missing_at_construction_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SourceFile("s", path=str(tmp_path / "nope.txt"))


def test_sourcefile_ref_path_rejected(tmp_path: Path) -> None:
    # A computed attribute of another resource is a Ref, unreadable at eval.
    other = File("f", path=str(tmp_path / "x.txt"), content="y")
    with pytest.raises(LanguageError):
        SourceFile("s", path=other.checksum)
