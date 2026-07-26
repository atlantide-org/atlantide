"""Committed stack outputs are retired, not accumulated.

A key left behind after its resource is gone still passes the "apply the source
stack first" check, so a dependent stack resolves a reference to something that no
longer exists.
"""

from __future__ import annotations

from atlantide.state import MemoryStateBackend
from tests.support import box_harness

#: `output` is part of the sanctioned config API, so it is imported like config does.
_OUT_CFG = "from atlantide.core import output\na = Box('a', size=1)\noutput('arn', a.out)\n"


def test_destroying_a_stack_retires_its_committed_outputs() -> None:
    """A stale key passes the "apply the source stack first" check while the
    referenced resource is gone, and the dependent stack resolves to it."""
    h = box_harness(MemoryStateBackend())
    h.apply(_OUT_CFG)
    assert h.backend.outputs() == {"default:arn": "a:1"}

    # Every node destroyed, and nothing exported any more.
    h.apply("from atlantide.core import output\n")
    assert h.backend.outputs() == {}


def test_removing_one_output_retires_just_that_key() -> None:
    h = box_harness(MemoryStateBackend())
    h.apply(_OUT_CFG + "output('name', 'a')\n")
    assert set(h.backend.outputs()) == {"default:arn", "default:name"}

    h.apply(_OUT_CFG)
    assert set(h.backend.outputs()) == {"default:arn"}


def test_another_stacks_outputs_are_left_alone() -> None:
    h = box_harness(MemoryStateBackend())
    h.backend.set_outputs({"other:vpc_id": "vpc-123"})
    h.apply(_OUT_CFG)
    assert h.backend.outputs()["other:vpc_id"] == "vpc-123"
