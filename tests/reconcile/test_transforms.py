"""Output transform combinators: deferred pure ops over apply-time Ref values."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atlantide.engine import Engine
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.reconcile import Action
from atlantide.reconcile.resolve import resolve_value
from tests.conftest import make_engine


def _engine() -> Engine:
    return make_engine(local.TYPES, LocalProvider())


def _config(tmp: Path, expr: str) -> str:
    return (
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp / 'a.txt')!r}, content='alpha')\n"
        f"File('b', path={str(tmp / 'b.txt')!r}, content={expr})\n"
    )


async def test_concat_over_ref_resolves_at_apply(tmp_path: Path) -> None:
    engine = _engine()
    cfg = _config(tmp_path, "concat(a.checksum, '!')")

    # The transform wires a real dependency edge a -> b.
    compiled = engine.compile(cfg).unwrap()
    b = compiled.ir.node("default:local.File:b")
    assert b is not None and "default:local.File:a" in b.dependencies

    (await engine.apply(cfg)).unwrap()
    checksum = hashlib.sha256(b"alpha").hexdigest()
    assert (tmp_path / "b.txt").read_text() == f"{checksum}!"

    # Re-apply unchanged -> NOOP (transform marker hashes stably).
    report = (await engine.apply(cfg)).unwrap()
    assert {c.action for c in engine.plan(cfg).unwrap().changeset} == {Action.NOOP}
    assert len(report.noop) == 2


async def test_interpolate_and_join(tmp_path: Path) -> None:
    engine = _engine()
    cfg = _config(tmp_path, "interpolate('[{}]', a.checksum)")
    (await engine.apply(cfg)).unwrap()
    checksum = hashlib.sha256(b"alpha").hexdigest()
    assert (tmp_path / "b.txt").read_text() == f"[{checksum}]"

    engine2 = _engine()
    cfg2 = _config(tmp_path, "join('/', ['x', a.checksum, 'y'])")
    (await engine2.apply(cfg2)).unwrap()
    assert (tmp_path / "b.txt").read_text() == f"x/{checksum}/y"


def test_unknown_transform_op_raises() -> None:
    from atlantide.core.errors import ProviderError

    with pytest.raises(ProviderError, match="unknown transform op 'bogus'"):
        resolve_value({"$transform": {"op": "bogus", "args": ["x"]}}, {})


def test_transform_ir_is_deterministic(tmp_path: Path) -> None:
    engine = _engine()
    cfg = _config(tmp_path, "concat(a.checksum, '/', 'x')")
    h1 = engine.compile(cfg).unwrap().hashes
    h2 = engine.compile(cfg).unwrap().hashes
    assert h1 == h2  # byte-stable content hash across compiles


def test_resolve_nested_transform() -> None:
    outputs = {"n": {"arn": "arn:aws:s3:::bucket"}}
    marker = {
        "$transform": {
            "op": "concat",
            "args": [{"$ref": "n#arn"}, {"$transform": {"op": "concat", "args": ["/", "*"]}}],
        }
    }
    assert resolve_value(marker, outputs) == "arn:aws:s3:::bucket/*"
