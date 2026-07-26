"""A property that is absent from prior state is not a property holding ``None``.

Reading a missing key as ``None`` makes a newly-added field look unchanged, which
matters because ``immutable()`` decides REPLACE from the changed-field set: the
resource then reaches ``update()`` with nothing to update instead of being rebuilt.
"""

from __future__ import annotations

from dataclasses import replace

from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend
from tests.support import box_harness


def test_a_new_immutable_field_defaulting_to_none_still_replaces() -> None:
    """Reading a missing prior key as None makes an added field look unchanged, so
    an `immutable()` one reaches `update()` with an empty change set rather than
    REPLACE."""
    h = box_harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    node = h.backend.load().nodes["default:test.Box:a"]
    # State written before the field existed: the property is absent, and the
    # hash it was stored under did not cover it either.
    h.backend.put(
        replace(
            node,
            input_hash="written-before-label-existed",
            properties={k: v for k, v in node.properties.items() if k != "label"},
        )
    )

    change = next(iter(h.diff_only("Box('a', size=1)\n")))
    assert change.action is not Action.NOOP
    assert "label" in change.changed_fields
