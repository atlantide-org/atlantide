"""The approved plan is the applied plan, or the apply refuses.

An apply re-diffs once it holds the lock — it must, or a resource another run
created in the meantime would still be planned as a CREATE and get built twice.
That re-diff is correct and is defended by ``test_concurrency``. What it also
means is that the changeset a human read and the changeset that executes can
differ, and nothing used to say so.
"""

from __future__ import annotations

import pytest
from returns.result import Success

from atlantide.core.errors import PlanDriftError
from atlantide.reconcile import Action, ChangeSet
from atlantide.reconcile.diff import Change
from atlantide.state import MemoryStateBackend
from tests.support import Box, engine_for, globals_of, state_node

GLOBALS = globals_of(Box)
ONE = "Box('a', size=1)\n"
TWO = "Box('a', size=1)\nBox('b', size=2)\n"


def _change(node_id: str, action: Action, **kw: object) -> Change:
    return Change(node_id=node_id, action=action, **kw)  # type: ignore[arg-type]


def _stored(name: str, *, size: int) -> object:
    """A state row a *different* run would have written: real properties, so the
    re-diff can reconstruct the resource, and a hash that forces an action."""
    return state_node(
        name,
        type="test.Box",
        input_hash="written-by-another-run",
        properties={"size": size},
        outputs={"out": f"{name}#1"},
    )


# -- the fingerprint ----------------------------------------------------------


def test_the_fingerprint_ignores_noop_nodes() -> None:
    """A NOOP is the absence of an action. Including them would make an unrelated
    node settling look like the approved plan had changed."""
    without = ChangeSet(changes=(_change("a", Action.CREATE),))
    with_noop = ChangeSet(changes=(_change("a", Action.CREATE), _change("b", Action.NOOP)))
    assert without.fingerprint() == with_noop.fingerprint()


def test_the_fingerprint_ignores_ordering() -> None:
    """The plan is a graph; whichever order the scheduler picks is not something
    the operator approved."""
    forward = ChangeSet(changes=(_change("a", Action.CREATE), _change("b", Action.UPDATE)))
    reversed_ = ChangeSet(changes=(_change("b", Action.UPDATE), _change("a", Action.CREATE)))
    assert forward.fingerprint() == reversed_.fingerprint()


def test_the_fingerprint_distinguishes_what_a_reviewer_would_care_about() -> None:
    base = ChangeSet(changes=(_change("a", Action.UPDATE, changed_fields=("size",)),))
    assert base.fingerprint() != ChangeSet(changes=(_change("a", Action.DELETE),)).fingerprint(), (
        "a different action"
    )
    assert (
        base.fingerprint()
        != ChangeSet(
            changes=(_change("a", Action.UPDATE, changed_fields=("size", "name")),)
        ).fingerprint()
    ), "different fields"
    assert (
        base.fingerprint()
        != ChangeSet(changes=(_change("b", Action.UPDATE, changed_fields=("size",)),)).fingerprint()
    ), "a different node"


# -- end to end ---------------------------------------------------------------


async def test_an_apply_refuses_a_changeset_that_is_no_longer_the_approved_one() -> None:
    """The scenario: a plan is shown and approved, another run creates one of its
    nodes, and the re-diff drops that CREATE. Applying anyway would be applying
    something nobody read."""
    backend = MemoryStateBackend()
    engine = engine_for(Box, backend=backend)

    approved = engine.plan(TWO, extra_globals=GLOBALS).unwrap().changeset
    assert len(approved.actionable) == 2

    # Another run lands `b` in between.
    backend.put(_stored("b", size=2))

    # Raised rather than returned: this happens inside the state lock, where the
    # engine's Result channel is not available (see the module docstring). The CLI
    # turns it back into a Failure at the `run_async` boundary.
    with pytest.raises(PlanDriftError) as caught:
        await engine.apply(TWO, extra_globals=GLOBALS, expect=approved)

    assert "default:test.Box:b" in str(caught.value)
    assert "Re-run" in str(caught.value)


async def test_an_unchanged_plan_applies_normally() -> None:
    """The guard must be invisible when nothing moved, or it would block every
    ordinary apply."""
    engine = engine_for(Box, backend=MemoryStateBackend())
    approved = engine.plan(TWO, extra_globals=GLOBALS).unwrap().changeset

    result = await engine.apply(TWO, extra_globals=GLOBALS, expect=approved)

    assert isinstance(result, Success), result
    assert len(result.unwrap().created) == 2


async def test_without_expect_the_re_diff_still_wins() -> None:
    """The opt-out (`--allow-plan-drift`) keeps the old behaviour: re-diff and
    apply whatever is now correct."""
    backend = MemoryStateBackend()
    engine = engine_for(Box, backend=backend)
    engine.plan(TWO, extra_globals=GLOBALS).unwrap()
    backend.put(_stored("b", size=2))

    result = await engine.apply(TWO, extra_globals=GLOBALS)  # no expect=

    assert isinstance(result, Success), result


async def test_a_node_appearing_in_the_fresh_plan_is_reported_too() -> None:
    """Drift in the other direction: something the approved plan did *not*
    include now needs doing. Applying it silently is the destroy-nobody-saw case.
    """
    backend = MemoryStateBackend()
    engine = engine_for(Box, backend=backend)

    approved = engine.plan(ONE, extra_globals=GLOBALS).unwrap().changeset
    # Another run leaves behind a node this config does not declare -> a DELETE
    # the operator never saw.
    backend.put(_stored("stray", size=9))

    with pytest.raises(PlanDriftError) as caught:
        await engine.apply(ONE, extra_globals=GLOBALS, expect=approved)

    assert "stray" in str(caught.value)
    assert "now also" in str(caught.value)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
