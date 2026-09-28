"""Laws of the diff, over generated IR and generated prior state.

The diff's central claim is convergence: after applying a plan, the next plan
has nothing left to do. That makes a re-run safe after an interruption, lets CI
check "nothing changed" by planning, and keeps a resource from being replaced on
every deploy. `tests/reconcile/test_diff.py` covers the classification table by
example; these properties cover the state space.

Convergence is a *fixed point*. Diffing against the state a clean apply would
leave is nearly a tautology, since the diff NOOPs on hash equality and the model
set the hash. These properties instead start from an arbitrary prior state, diff,
apply *only the changes that diff produced*, and diff again. That reaches the
branches hash equality alone cannot:

* a row poisoned by ``refresh --write``, which no digest equals;
* a row left ``creating`` by an apply that died between the write-ahead and the
  confirm, which must re-create rather than NOOP;
* the **stale cascade**: a CREATE or REPLACE hands every transitive dependent a
  new physical id, so they must act even though their own hashes never moved.
  It is the only diff rule that is not a function of one node's own state.

The model of what an apply persists (``applied_row``) is a hand copy of the
executor's; if the executor's row changes, a disagreeing model keeps passing.
The first test pins the model against a real apply, and is deliberately not a
property.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core.actions import Action
from atlantide.ir.model import IRGraph
from atlantide.reconcile.changes import ChangeSet, restrict
from atlantide.reconcile.diff import diff
from atlantide.state import MemoryStateBackend, StateGraph
from tests.support.strategies import (
    applied_row,
    hashes_for,
    ir_graphs,
    mutabilities,
    prior_states,
)

from .conftest import Harness

TWO_NODES = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"

#: What the diff reads off a state row; the model must be faithful only on these.
#: `outputs` and `secret_digests` come from the provider and the keyfile, and no
#: branch of `diff` reads either.
DIFF_RELEVANT = (
    "id",
    "type",
    "provider",
    "provider_version",
    "input_hash",
    "properties",
    "dependencies",
    "prevent_destroy",
    "status",
)


def test_the_model_of_an_applied_row_matches_what_the_executor_persists() -> None:
    """Not a property: a real compile, apply, and state read.

    `applied_row` is a hand copy of `NodeRecords.state_node`, and every property
    below relies on it. If the executor persisted something else (e.g. resolved
    rather than symbolic properties), the model would agree with itself and the
    convergence properties would pass while convergence was broken.
    """
    harness = Harness(MemoryStateBackend())
    harness.apply(TWO_NODES)
    _, ir, _, hashes = harness._compile(TWO_NODES, harness._providers())

    persisted = harness.backend.load().nodes

    assert set(persisted) == {node.id for node in ir.nodes}
    for node in ir.nodes:
        modelled = applied_row(node, hashes)
        for name in DIFF_RELEVANT:
            assert getattr(modelled, name) == getattr(persisted[node.id], name), name


# -- the model ---------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One config, its hashes, a state to diff it against, and its mutability.

    Carries the two operations every property needs: ``diff`` and the model of
    what an apply persists.
    """

    ir: IRGraph
    hashes: dict[str, str]
    prior: StateGraph
    mutability: dict[str, object]

    def diff(
        self, prior: StateGraph | None = None, *, replace: frozenset[str] = frozenset()
    ) -> ChangeSet:
        """The plan against ``prior``, defaulting to the scenario's own state."""
        return diff(
            self.ir,
            self.hashes,
            self.prior if prior is None else prior,
            self.mutability,
            replace=replace,
        )

    def after(self, changeset: ChangeSet, prior: StateGraph | None = None) -> StateGraph:
        """The state a fully successful apply of ``changeset`` leaves behind.

        A NOOP writes nothing (the Merkle skip). Modelling it as a rewrite would
        hide bugs where the next diff converges only because every row was
        rewritten.
        """
        by_id = {node.id: node for node in self.ir.nodes}
        nodes = dict((self.prior if prior is None else prior).nodes)
        for change in changeset:
            if change.action is Action.DELETE:
                nodes.pop(change.node_id, None)
            elif change.action is not Action.NOOP:
                nodes[change.node_id] = applied_row(by_id[change.node_id], self.hashes)
        return StateGraph(nodes=nodes)


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    ir = draw(ir_graphs())
    hashes = hashes_for(ir)
    return Scenario(
        ir=ir,
        hashes=hashes,
        prior=draw(prior_states(ir, hashes)),
        mutability=draw(mutabilities(ir)),
    )


# -- convergence -------------------------------------------------------------


@given(scenarios())
def test_applying_a_plan_leaves_nothing_to_do(scenario: Scenario) -> None:
    """Convergence, from any starting state.

    Otherwise the plan after a successful apply still shows work, and a real
    change is indistinguishable from a failure to settle.
    """
    first = scenario.diff()

    assert scenario.diff(scenario.after(first)).actionable == []


@given(scenarios())
def test_recreating_a_node_pulls_every_dependent_out_of_noop(scenario: Scenario) -> None:
    """The stale cascade fires and reaches every transitive dependent.

    A node recreated for a state-side reason (missing from state, or an
    unconfirmed create) keeps its config, so every dependent hashes identically
    and the Merkle skip would NOOP them while the provider hands out a new
    physical id. Their resolved inputs moved even though their hashes did not.

    Asserted directly rather than inferred from convergence: a cascade that
    stopped one level short would still converge, leaving a dependent pointing at
    an id that no longer exists.
    """
    first = scenario.diff()
    reidentified = {
        change.node_id for change in first if change.action in (Action.CREATE, Action.REPLACE)
    }

    dependents: dict[str, list[str]] = {}
    for node in scenario.ir.nodes:
        for edge in node.edges():
            dependents.setdefault(edge, []).append(node.id)
    stale: set[str] = set()
    queue = list(reidentified)
    while queue:
        for child in dependents.get(queue.pop(), ()):
            if child not in stale:
                stale.add(child)
                queue.append(child)

    assert stale <= {change.node_id for change in first.actionable}


@given(scenarios())
def test_the_diff_is_deterministic(scenario: Scenario) -> None:
    """Two diffs of the same inputs have the same fingerprint, which is what
    `--allow-plan-drift` compares a saved plan against."""
    assert scenario.diff().fingerprint() == scenario.diff().fingerprint()


@given(scenarios())
def test_every_id_in_either_side_gets_exactly_one_change(scenario: Scenario) -> None:
    """Totality: a dropped id would be a resource silently absent from the plan."""
    node_ids = [change.node_id for change in scenario.diff()]

    expected = {node.id for node in scenario.ir.nodes} | set(scenario.prior.nodes)
    assert sorted(node_ids) == sorted(expected)
    assert len(node_ids) == len(set(node_ids))


@given(ir_graphs())
def test_an_empty_prior_state_is_all_creates(ir: IRGraph) -> None:
    changeset = diff(ir, hashes_for(ir), StateGraph(), {})

    assert {c.action for c in changeset} == {Action.CREATE}


@given(scenarios())
def test_an_empty_desired_ir_is_all_deletes(scenario: Scenario) -> None:
    changeset = diff(IRGraph(nodes=()), {}, scenario.prior, scenario.mutability)

    assert {c.action for c in changeset} == ({Action.DELETE} if scenario.prior.nodes else set())


# -- targeting and forced replacement ----------------------------------------


class Selection(NamedTuple):
    """A changeset and the two id sets the CLI can narrow it with."""

    scenario: Scenario
    changeset: ChangeSet
    targeted: frozenset[str]
    replaced: frozenset[str]

    def forced(self) -> ChangeSet:
        """The plan with ``replaced`` forced, as the planner asks for it."""
        return self.scenario.diff(replace=self.replaced)


@st.composite
def selections(draw: st.DrawFn) -> Selection:
    """A changeset plus a `--target` and a `--replace` drawn from its own ids."""
    scenario = draw(scenarios())
    changeset = scenario.diff()
    ids = sorted({change.node_id for change in changeset})
    return Selection(
        scenario=scenario,
        changeset=changeset,
        targeted=frozenset(draw(st.lists(st.sampled_from(ids), max_size=3, unique=True))),
        replaced=frozenset(draw(st.lists(st.sampled_from(ids), max_size=2, unique=True))),
    )


@given(selections())
def test_targeting_acts_on_exactly_the_selection(case: Selection) -> None:
    """`--target` keeps exactly the selected changes, asserted as an equality.

    A subset assertion would pass for an implementation that dropped changes it
    should have kept, which is as wrong as acting outside the selection.
    """
    restricted = restrict(case.changeset, case.targeted)

    assert restricted.fingerprint() == frozenset(
        entry for entry in case.changeset.fingerprint() if entry[0] in case.targeted
    )


@given(selections())
def test_a_forced_replace_cannot_escape_a_target_selection(case: Selection) -> None:
    """Composed in the planner's order: `diff(replace=)`, then restrict.

    The reverse order would let `--replace` bypass `--target` and replace a
    resource the operator excluded.
    """
    composed = restrict(case.forced(), case.targeted)

    assert {change.node_id for change in composed.actionable} <= case.targeted


@given(selections())
def test_restricting_twice_changes_nothing(case: Selection) -> None:
    once = restrict(case.changeset, case.targeted)

    assert restrict(once, case.targeted) == once


@given(selections())
def test_restricting_to_the_whole_changeset_is_the_identity(case: Selection) -> None:
    """The un-targeted run is the same code path as a targeted one, so it must be
    the same plan — otherwise `--target '*'` and no `--target` differ."""
    every_id = frozenset(change.node_id for change in case.changeset)

    assert restrict(case.changeset, every_id) == case.changeset


@given(selections())
def test_forcing_a_node_already_replaced_or_deleted_changes_nothing(case: Selection) -> None:
    """Idempotence: naming a node the plan already recreates or drops is a no-op."""
    settled = frozenset(
        c.node_id for c in case.changeset if c.action in (Action.REPLACE, Action.DELETE)
    )

    assert case.scenario.diff(replace=settled) == case.changeset


@given(selections())
def test_every_forced_node_on_both_sides_is_replaced(case: Selection) -> None:
    """A named node that config declares and state records is replaced, whatever
    the diff decided. (Its dependents may change too: the refinement pass
    re-examines them on purpose, so they are not asserted untouched.)"""
    for change in case.forced():
        if change.node_id in case.replaced and change.desired and change.prior:
            assert change.action is Action.REPLACE


@given(selections())
def test_forcing_a_replace_never_downgrades_a_pending_delete(case: Selection) -> None:
    """A DELETE stays a DELETE; turning it into a REPLACE would re-create a
    resource the config does not declare."""
    deleting = {c.node_id for c in case.changeset if c.action is Action.DELETE}

    assert {c.node_id for c in case.forced() if c.action is Action.DELETE} == deleting


@given(scenarios())
def test_an_ignored_field_never_produces_a_change(scenario: Scenario) -> None:
    """`ignore_changes` is applied independently by the Merkle hash and by the
    diff, and both must agree. Otherwise the hash would move while the diff
    reported no changed field: an UPDATE with an empty reason on every plan."""
    ignored = IRGraph(
        nodes=tuple(
            replace(node, ignore_changes=tuple(node.properties)) for node in scenario.ir.nodes
        )
    )
    hashes = hashes_for(ignored)
    prior = StateGraph(
        nodes={
            node.id: replace(applied_row(node, hashes), properties={"anything": "else"})
            for node in ignored.nodes
        }
    )

    for change in diff(ignored, hashes, prior, scenario.mutability):
        assert change.changed_fields == ()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
