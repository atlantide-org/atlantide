"""A node that never finishes fails the apply instead of hanging it forever.

Without a ceiling, a provider call that never answers keeps the run alive
indefinitely — holding its lease the whole time, which does *not* also hang: the
lease lapses, someone else takes it, and now two runs believe they own the same
resources. A hang is therefore not merely a stalled apply; it is the setup for
the divergence everything else here is built to prevent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from atlantide.core.errors import ProviderError
from atlantide.reconcile.context import DEFAULT_NODE_TIMEOUT
from atlantide.state import MemoryStateBackend
from tests.support import FakeProvider

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


class NeverFinishes(FakeProvider):
    """A provider whose create never returns for the named node."""

    def __init__(self, stuck: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.stuck = stuck

    async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
        if res.node_id == self.stuck:
            await asyncio.sleep(3600)
        return await super().create(ctx, res)


def _harness(stuck: str, timeout: float = 0.05) -> Harness:
    h = Harness(MemoryStateBackend(), provider=NeverFinishes(stuck))
    h.node_timeout = timeout
    return h


def test_a_stuck_node_fails_rather_than_hanging_the_apply() -> None:
    h = _harness(A)

    with pytest.raises(ExceptionGroup) as caught:
        h.apply("Box('a', size=1)\n")

    from atlantide.cli.errors import flatten_group

    leaves = flatten_group(caught.value)
    error = next(e for e in leaves if isinstance(e, ProviderError))
    assert error.node_id == A, "the timeout is attributed to the node that stalled"
    assert error.op == "create"


def test_a_timeout_is_a_node_failure_not_an_interrupt() -> None:
    """`asyncio.timeout` raises `TimeoutError`, not `CancelledError`, precisely so
    it reads as this node failing — which is what lets the saga treat it like any
    other provider failure rather than like a Ctrl-C."""
    h = _harness(A)

    with pytest.raises(ExceptionGroup) as caught:
        h.apply("Box('a', size=1)\n")

    from atlantide.cli.errors import flatten_group

    leaves = flatten_group(caught.value)
    assert not any(isinstance(e, asyncio.CancelledError) for e in leaves)


def test_a_stuck_node_triggers_the_saga_for_its_siblings() -> None:
    """The point of making a timeout an ordinary failure: everything already
    built gets compensated, instead of being stranded by a hang."""
    h = _harness(B)

    with pytest.raises(ExceptionGroup):
        h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="rollback")

    assert A not in h.backend.load().nodes
    assert h.fake().deleted == ["a"]


def test_a_node_within_its_budget_is_untouched() -> None:
    """The ceiling must be invisible to an apply that simply takes a while."""
    h = Harness(MemoryStateBackend())
    h.node_timeout = 30.0
    report = h.apply("Box('a', size=1)\n")
    assert report.created == [A]


def test_the_default_budget_clears_the_slowest_real_wait() -> None:
    """CloudFront polls up to 30 minutes for a distribution to deploy. A default
    below that would fail correct applies, which is worse than not having one."""
    from atlantide.providers.aws.handlers.cloudfront import (
        _DEPLOY_POLL_ATTEMPTS,
        _DEPLOY_POLL_DELAY,
    )

    assert DEFAULT_NODE_TIMEOUT > _DEPLOY_POLL_ATTEMPTS * _DEPLOY_POLL_DELAY


def test_a_stuck_delete_also_fails(tmp_path: object) -> None:
    """Destroy is the other direction and hangs just as easily."""

    class StuckDelete(FakeProvider):
        async def delete(self, ctx: Any, res: Any) -> None:
            await asyncio.sleep(3600)

    h = Harness(MemoryStateBackend(), provider=StuckDelete())
    h.apply("Box('a', size=1)\n")
    h.node_timeout = 0.05

    with pytest.raises(ExceptionGroup) as caught:
        h.apply("")  # dropped from config -> DELETE

    from atlantide.cli.errors import flatten_group

    error = next(e for e in flatten_group(caught.value) if isinstance(e, ProviderError))
    assert error.op == "delete"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
