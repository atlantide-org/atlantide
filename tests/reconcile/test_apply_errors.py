"""Apply failures carry the failing node id, op, and the original cause.

These tests pin that context onto the raised ``ProviderError`` so a failure
traces back to its origin rather than surfacing as a bare provider message.
"""

from __future__ import annotations

import pytest

from atlantide.core.errors import ProviderError
from atlantide.state import MemoryStateBackend
from tests.support import leaves

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


def test_failed_create_carries_node_id_op_and_cause(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("a")  # the fake provider raises RuntimeError
    with pytest.raises(ExceptionGroup) as ei:
        h.apply("Box('a', size=1)\n")

    [err] = leaves(ei.value)
    assert isinstance(err, ProviderError)
    assert err.node_id == A
    assert err.op == "create"
    # The original provider exception is preserved as the cause, not stringified.
    assert isinstance(err.__cause__, RuntimeError)
    assert "create failed for a" in str(err.__cause__)


def test_each_failed_node_is_tagged(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.update({"a", "b"})  # two independent nodes fail
    with pytest.raises(ExceptionGroup) as ei:
        h.apply("Box('a', size=1)\nBox('b', size=2)\n")

    tagged = {e.node_id: e for e in leaves(ei.value) if isinstance(e, ProviderError)}
    assert set(tagged) == {A, B}
    assert all(e.op == "create" for e in tagged.values())
