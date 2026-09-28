"""An external stack output nested in a transform resolves at apply.

``resolve_stack_refs`` replaced only a field that *is* a ``StackOutputRef``, so
``concat("x-", StackReference("network").output("net_id"))`` reached the
provider as an unevaluated ``Transform``. The in-config case is inlined into a
Ref before lowering (core/inline.py); this is the separately-applied one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from atlantide.engine import Engine
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend
from tests.conftest import make_engine

NETWORK = (
    "from atlantide.core import Stack, output\n"
    "with Stack('network', region='local'):\n"
    "    output('net_id', 'vpc-123')\n"
)


def _engine() -> Engine:
    return make_engine(
        local.TYPES, LocalProvider(allow_outside_project=True), backend=MemoryStateBackend()
    )


def _app(content: str, *, src: Path | None = None, dst: Path) -> str:
    source = (
        "from atlantide.core import Stack, StackReference, concat\n"
        "from atlantide.providers.local import File\n"
        "with Stack('app', region='local'):\n"
    )
    if src is not None:
        source += f"    src = File('src', path={str(src)!r}, content='hello')\n"
    return source + f"    File('cfg', path={str(dst)!r}, content={content})\n"


async def test_a_nested_external_stack_output_resolves(tmp_path: Path) -> None:
    engine = _engine()
    (await engine.apply(NETWORK)).unwrap()
    dst = tmp_path / "app.txt"
    app = _app("concat('x-', StackReference('network').output('net_id'))", dst=dst)

    (await engine.apply(app)).unwrap()

    assert dst.read_text() == "x-vpc-123"
    changes = engine.plan(app).unwrap().changeset
    assert {c.action for c in changes} == {Action.NOOP}


async def test_a_stack_output_beside_a_ref_in_one_transform_resolves(tmp_path: Path) -> None:
    """``resolve_refs`` evaluates the transform, so the stack output must already
    be in place by then, not stringified as a handle."""
    engine = _engine()
    (await engine.apply(NETWORK)).unwrap()
    dst = tmp_path / "app.txt"
    app = _app(
        "concat(src.checksum, ':', StackReference('network').output('net_id'))",
        src=tmp_path / "src.txt",
        dst=dst,
    )

    (await engine.apply(app)).unwrap()

    assert dst.read_text() == f"{hashlib.sha256(b'hello').hexdigest()}:vpc-123"
