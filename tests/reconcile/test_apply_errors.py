"""Apply failures carry the failing node id, op, and the original cause.

The executor knows exactly which node broke; these tests pin that context onto
the raised ``ProviderError`` so a developer can trace a failure back to its
origin instead of getting a bare provider message.
"""

from __future__ import annotations

import pytest

from atlantide.core.errors import ProviderError
from atlantide.state import MemoryStateBackend

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


def _leaves(group: BaseException) -> list[BaseException]:
    excs = getattr(group, "exceptions", None)
    return [leaf for e in excs for leaf in _leaves(e)] if excs else [group]


def test_failed_create_carries_node_id_op_and_cause(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("a")  # MockProvider raises RuntimeError
    with pytest.raises(ExceptionGroup) as ei:
        h.apply("Box('a', size=1)\n")

    leaves = _leaves(ei.value)
    assert len(leaves) == 1
    err = leaves[0]
    assert isinstance(err, ProviderError)
    assert err.node_id == A
    assert err.op == "create"
    # the original provider exception is preserved as the cause, not stringified away
    assert isinstance(err.__cause__, RuntimeError)
    assert "create failed for a" in str(err.__cause__)


def test_each_failed_node_is_tagged(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.update({"a", "b"})  # two independent nodes fail
    with pytest.raises(ExceptionGroup) as ei:
        h.apply("Box('a', size=1)\nBox('b', size=2)\n")

    tagged = {e.node_id: e for e in _leaves(ei.value) if isinstance(e, ProviderError)}
    assert set(tagged) == {A, B}
    assert all(e.op == "create" for e in tagged.values())
