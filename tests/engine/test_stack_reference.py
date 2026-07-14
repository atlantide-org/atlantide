"""StackReference: one stack reads another stack's outputs.

Two regimes: an *external* reference (source stack in a separate config, already
applied) resolves from committed state; an *in-config* reference (source stack in
the same config) is inlined into a real graph edge and ordered automatically.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from returns.result import Failure, Success

from atlantide.core import StackOutputCycleError
from atlantide.engine import Engine
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.state import MemoryStateBackend
from tests.conftest import make_engine


def _engine(backend: MemoryStateBackend) -> Engine:
    return make_engine(local.TYPES, LocalProvider(), backend=backend)


def _network_cfg() -> str:
    return (
        "from atlantide.core import Stack, output\n"
        "with Stack('network', region='local'):\n"
        "    output('net_id', 'vpc-123')\n"
    )


def _app_cfg(path: Path) -> str:
    return (
        "from atlantide.core import Stack, StackReference\n"
        "from atlantide.providers.local import File\n"
        "with Stack('app', region='local'):\n"
        f"    File('cfg', path={str(path)!r}, "
        "content=StackReference('network').output('net_id'))\n"
    )


async def test_stack_reference_resolves_committed_output(tmp_path: Path) -> None:
    backend = MemoryStateBackend()
    engine = _engine(backend)

    # apply the network stack -> its output is committed to state
    (await engine.apply(_network_cfg())).unwrap()
    assert backend.outputs()["network:net_id"] == "vpc-123"

    # the app stack reads it via StackReference -> resolved to the real value at apply
    target = tmp_path / "app.txt"
    (await engine.apply(_app_cfg(target))).unwrap()
    assert target.read_text() == "vpc-123"


async def test_plan_fails_when_referenced_stack_output_missing(tmp_path: Path) -> None:
    # apply the app stack WITHOUT the network stack -> its output isn't committed
    engine = _engine(MemoryStateBackend())
    result = engine.plan(_app_cfg(tmp_path / "app.txt"))
    assert isinstance(result, Failure)
    message = str(result.failure())
    assert "undefined stack output" in message
    assert "network:net_id" in message


# -- in-config cross-stack references (auto-ordered via the graph) ------------


def _inconfig_cfg(src: Path, dst: Path) -> str:
    """One config: `common`'s File.checksum is exported and consumed by `app`."""
    return (
        "from atlantide.core import Stack, StackReference, output\n"
        "from atlantide.providers.local import File\n"
        "with Stack('common', region='local'):\n"
        f"    src = File('src', path={str(src)!r}, content='hello')\n"
        "    output('cksum', src.checksum)\n"
        "with Stack('app', region='local'):\n"
        f"    File('cfg', path={str(dst)!r}, "
        "content=StackReference('common').output('cksum'))\n"
    )


async def test_inconfig_cross_stack_plan_clean_slate_no_error(tmp_path: Path) -> None:
    # On an empty backend the plan must NOT hard-fail: the in-config ref is a real
    # edge, so `app`'s node plans as CREATE depending on `common`'s node.
    engine = _engine(MemoryStateBackend())
    result = engine.plan(_inconfig_cfg(tmp_path / "src.txt", tmp_path / "cfg.txt"))
    assert isinstance(result, Success)
    plan = result.unwrap()
    app = next(n for n in plan.compiled.ir.nodes if n.id == "app:local.File:cfg")
    assert "common:local.File:src" in app.dependencies
    assert app.properties["content"] == {"$ref": "common:local.File:src#checksum"}


async def test_inconfig_cross_stack_apply_orders_and_resolves(tmp_path: Path) -> None:
    engine = _engine(MemoryStateBackend())
    src, dst = tmp_path / "src.txt", tmp_path / "cfg.txt"
    report = (await engine.apply(_inconfig_cfg(src, dst))).unwrap()
    # common's resource applies before app's, in one run.
    assert report.created.index("common:local.File:src") < report.created.index(
        "app:local.File:cfg"
    )
    # app received common's real computed value.
    assert dst.read_text() == hashlib.sha256(b"hello").hexdigest()


async def test_independent_stacks_apply_in_one_run(tmp_path: Path) -> None:
    cfg = (
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File\n"
        "with Stack('a', region='local'):\n"
        f"    File('one', path={str(tmp_path / 'a.txt')!r}, content='a')\n"
        "with Stack('b', region='local'):\n"
        f"    File('two', path={str(tmp_path / 'b.txt')!r}, content='b')\n"
    )
    report = (await _engine(MemoryStateBackend()).apply(cfg)).unwrap()
    assert set(report.created) == {"a:local.File:one", "b:local.File:two"}


async def test_output_returns_reusable_handle(tmp_path: Path) -> None:
    # Consuming `output()`'s return value (no repeated string) is equivalent to a
    # StackReference: it inlines to a real edge and resolves to the producer's value.
    cfg = (
        "from atlantide.core import Stack, output\n"
        "from atlantide.providers.local import File\n"
        "with Stack('common', region='local'):\n"
        f"    src = File('src', path={str(tmp_path / 'src.txt')!r}, content='hello')\n"
        "    cksum = output('cksum', src.checksum)\n"
        "with Stack('app', region='local'):\n"
        f"    File('cfg', path={str(tmp_path / 'cfg.txt')!r}, content=cksum)\n"
    )
    engine = _engine(MemoryStateBackend())
    plan = engine.plan(cfg).unwrap()
    app = next(n for n in plan.compiled.ir.nodes if n.id == "app:local.File:cfg")
    assert "common:local.File:src" in app.dependencies  # handle became a real edge
    (await _engine(MemoryStateBackend()).apply(cfg)).unwrap()
    assert (tmp_path / "cfg.txt").read_text() == hashlib.sha256(b"hello").hexdigest()


def test_output_handle_equals_stack_reference() -> None:
    # The returned handle is exactly what StackReference(<this stack>).output(name)
    # produces, so the two consumption styles are interchangeable.
    from atlantide.core import Stack, StackReference, collecting, output

    with collecting(), Stack("common", region="local"):
        handle = output("vpc_id", "vpc-123")
    assert handle == StackReference("common").output("vpc_id")


async def test_inconfig_cycle_detected() -> None:
    cfg = (
        "from atlantide.core import Stack, StackReference, output\n"
        "with Stack('a', region='local'):\n"
        "    output('x', StackReference('b').output('y'))\n"
        "with Stack('b', region='local'):\n"
        "    output('y', StackReference('a').output('x'))\n"
    )
    result = _engine(MemoryStateBackend()).plan(cfg)
    assert isinstance(result, Failure)
    assert isinstance(result.failure(), StackOutputCycleError)
    assert "cross-stack output cycle" in str(result.failure())
