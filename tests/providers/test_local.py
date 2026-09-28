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

    target.write_text("v2")
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


# -- project root: resolution and confinement ---------------------------------


async def _op(provider: LocalProvider, op: str, res: File | SourceFile) -> object:
    """Run one provider operation on ``res`` (``update`` from an empty prior)."""
    if op == "update":
        return await provider.update(Context(), {}, res)
    return await getattr(provider, op)(Context(), res)


def _project(tmp_path: Path, toml: str = "") -> Path:
    """A project root holding an ``atlantide.toml``, plus a ``sub`` directory."""
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "atlantide.toml").write_text(toml)
    return root


async def test_relative_path_resolves_against_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.chdir(root / "sub")
    provider = LocalProvider(root)
    res = File("f", path="out/f.txt", content="hi")

    out = await provider.create(Context(), res)
    assert (root / "out" / "f.txt").read_text() == "hi"
    assert not (root / "sub" / "out").exists()
    assert out["path"] == "out/f.txt"  # state keeps the path as written

    monkeypatch.chdir(root)  # a later run from elsewhere sees the same file
    assert await provider.read(Context(), res) == out
    await provider.delete(Context(), res)
    assert not (root / "out" / "f.txt").exists()


@pytest.mark.parametrize("op", ["create", "read", "update", "delete"])
@pytest.mark.parametrize("escape", ["../outside.txt", "ABSOLUTE"])
async def test_file_escaping_root_is_rejected(tmp_path: Path, op: str, escape: str) -> None:
    root = _project(tmp_path)
    victim = tmp_path / "outside.txt"
    victim.write_text("precious")
    path = str(victim) if escape == "ABSOLUTE" else escape
    provider = LocalProvider(root)
    res = File("f", path=path, content="pwned")

    with pytest.raises(ProviderError, match="outside the project root") as ei:
        await _op(provider, op, res)
    assert ei.value.op == op
    assert victim.read_text() == "precious"  # neither overwritten nor unlinked


async def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    victim = tmp_path / "outside.txt"
    victim.write_text("precious")
    (root / "link.txt").symlink_to(victim)
    (root / "linkdir").symlink_to(tmp_path)
    provider = LocalProvider(root)

    for path in ("link.txt", "linkdir/outside.txt"):
        with pytest.raises(ProviderError, match="outside the project root"):
            await provider.create(Context(), File("f", path=path, content="pwned"))
        with pytest.raises(ProviderError, match="outside the project root"):
            await provider.delete(Context(), File("f", path=path))
    assert victim.read_text() == "precious"


async def test_symlink_within_root_is_allowed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "real").mkdir()
    (root / "alias").symlink_to(root / "real")
    await LocalProvider(root).create(Context(), File("f", path="alias/f.txt", content="x"))
    assert (root / "real" / "f.txt").read_text() == "x"


async def test_allow_outside_project_opts_out(tmp_path: Path) -> None:
    root = _project(tmp_path)
    provider = LocalProvider(root, allow_outside_project=True)
    await provider.create(Context(), File("f", path="../outside.txt", content="ok"))
    assert (tmp_path / "outside.txt").read_text() == "ok"


async def test_sourcefile_read_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2")
    # A pinned checksum (the rehydrate path) skips the eval-time read, so this
    # exercises the provider's own check — the one that guards what enters state.
    res = SourceFile("s", path=str(secret), checksum=_sum("hunter2"))
    for op in ("create", "read"):
        with pytest.raises(ProviderError, match="outside the project root"):
            await getattr(LocalProvider(root), op)(Context(), res)


def test_plugin_factory_reads_settings(tmp_path: Path) -> None:
    from atlantide.providers.local import PLUGIN

    confined = PLUGIN.factory({"root": str(tmp_path)})
    assert isinstance(confined, LocalProvider)
    assert confined.scope.root == tmp_path and confined.scope.confined
    opted_out = PLUGIN.factory({"root": str(tmp_path), "allow_outside_project": True})
    assert isinstance(opted_out, LocalProvider)
    assert not opted_out.scope.confined
    # Only a real boolean opts out; a stray string does not.
    stray = PLUGIN.factory({"root": str(tmp_path), "allow_outside_project": "yes"})
    assert isinstance(stray, LocalProvider) and stray.scope.confined
    # No project root known: the working directory stands in, still confined.
    bare = PLUGIN.factory({})
    assert isinstance(bare, LocalProvider) and bare.scope.confined
    assert bare.scope.root == Path.cwd() and bare.scope.implicit_root
    loose = PLUGIN.factory({"allow_outside_project": True})
    assert isinstance(loose, LocalProvider) and not loose.scope.confined


def test_sourcefile_eval_resolves_against_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "data.txt").write_text("at root")
    (root / "sub" / "data.txt").write_text("in sub")
    monkeypatch.chdir(root / "sub")
    assert SourceFile("s", path="data.txt").checksum == _sum("at root")


def test_sourcefile_eval_escaping_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (tmp_path / "secret.txt").write_text("hunter2")
    monkeypatch.chdir(root / "sub")
    with pytest.raises(LanguageError, match="outside the project root"):
        SourceFile("s", path="../../secret.txt")
    with pytest.raises(LanguageError, match="outside the project root"):
        SourceFile("s", path=str(tmp_path / "secret.txt"))


def test_sourcefile_eval_honours_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, "[provider.local]\nallow_outside_project = true\n")
    (tmp_path / "shared.txt").write_text("shared")
    monkeypatch.chdir(root)
    assert SourceFile("s", path="../shared.txt").checksum == _sum("shared")


def test_sourcefile_eval_without_project_is_cwd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data.txt").write_text("here")
    monkeypatch.chdir(tmp_path)  # explicit, though the autouse fixture does it too
    assert SourceFile("s", path="data.txt").checksum == _sum("here")


# -- no project file: the working directory is the root ----------------------


async def test_no_project_resolves_against_cwd_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "later").mkdir()
    monkeypatch.chdir(tmp_path)
    provider = LocalProvider()
    monkeypatch.chdir(tmp_path / "later")  # the boundary does not follow the cwd
    res = File("f", path="out/f.txt", content="hi")
    await provider.create(Context(), res)
    assert (tmp_path / "out" / "f.txt").read_text() == "hi"
    assert await provider.read(Context(), res) is not None
    await provider.delete(Context(), res)
    assert not (tmp_path / "out" / "f.txt").exists()


@pytest.mark.parametrize("op", ["create", "read", "update", "delete"])
@pytest.mark.parametrize("escape", ["../outside.txt", "ABSOLUTE"])
async def test_no_project_escaping_cwd_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str, escape: str
) -> None:
    (tmp_path / "work").mkdir()
    victim = tmp_path / "outside.txt"
    victim.write_text("precious")
    monkeypatch.chdir(tmp_path / "work")
    path = str(victim) if escape == "ABSOLUTE" else escape
    provider = LocalProvider()
    res = File("f", path=path, content="pwned")

    with pytest.raises(ProviderError, match="outside the working directory") as ei:
        await _op(provider, op, res)
    assert ei.value.op == op
    assert victim.read_text() == "precious"


async def test_no_project_sourcefile_escaping_cwd_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "work").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2")
    monkeypatch.chdir(tmp_path / "work")
    # Eval-time fingerprint.
    for path in ("../secret.txt", str(secret)):
        with pytest.raises(LanguageError, match="outside the working directory"):
            SourceFile("s", path=path)
    # Provider read (the rehydrate path skips the eval-time read).
    res = SourceFile("s", path=str(secret), checksum=_sum("hunter2"))
    for op in ("create", "read", "update"):
        with pytest.raises(ProviderError, match="outside the working directory"):
            await _op(LocalProvider(), op, res)


async def test_no_project_opt_out_allows_outside_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    provider = LocalProvider(allow_outside_project=True)
    res = File("f", path="../outside.txt", content="ok")
    await provider.create(Context(), res)
    assert (tmp_path / "outside.txt").read_text() == "ok"
    await provider.delete(Context(), res)
    assert not (tmp_path / "outside.txt").exists()

    (tmp_path / "shared.txt").write_text("shared")
    pinned = SourceFile("s", path=str(tmp_path / "shared.txt"), checksum=_sum("shared"))
    assert await provider.read(Context(), pinned) == {"content": "shared"}


def test_sourcefile_content_is_sensitive() -> None:
    from atlantide.core import is_sensitive

    assert is_sensitive(SourceFile, "content")


def test_cli_apply_from_subdir_then_destroy_from_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: runs from a subdirectory and from the root touch the same file."""
    from tests.support import Cli

    cli = Cli()
    root = _project(tmp_path, 'config = "infra.py"\nstate = "infra.db"\n')
    (root / "infra.py").write_text(
        "from atlantide.providers.local import File\nFile('f', path='out.txt', content='hi')\n"
    )
    monkeypatch.chdir(root / "sub")
    cli.ok("apply", "-y")
    assert (root / "out.txt").read_text() == "hi"
    assert not (root / "sub" / "out.txt").exists()

    monkeypatch.chdir(root)
    cli.ok("destroy", "-y")
    assert not (root / "out.txt").exists()


def test_cli_rejects_escape_unless_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support import Cli

    cli = Cli()
    root = _project(tmp_path, 'config = "infra.py"\nstate = "infra.db"\n')
    (root / "infra.py").write_text(
        "from atlantide.providers.local import File\n"
        "File('f', path='../outside.txt', content='x')\n"
    )
    monkeypatch.chdir(root)
    result = cli.run("apply", "-y")
    assert result.exit_code != 0
    assert "outside the project root" in result.output
    assert not (tmp_path / "outside.txt").exists()

    (root / "atlantide.toml").write_text(
        'config = "infra.py"\nstate = "infra.db"\n\n'
        "[provider.local]\nallow_outside_project = true\n"
    )
    cli.ok("apply", "-y")
    assert (tmp_path / "outside.txt").read_text() == "x"
