"""Plan refinement and policy evaluation: compiled config + prior state -> Plan.

Also the two checks made on a plan once the lease is held: the destroy
changeset, and refusing an apply whose changes drifted from the approved ones.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError, Resource
from atlantide.core.errors import PlanDriftError, SecretsError
from atlantide.core.markers import STACK_OUTPUT_KEY, is_stack_output_marker
from atlantide.engine.model import Compiled, Plan
from atlantide.engine.policy_eval import evaluate_policies
from atlantide.engine.result import catching, forward_failure
from atlantide.engine.secret_audit import audit_secrets
from atlantide.engine.selection import destroy_selection
from atlantide.graph.cbd import cbd_forcers, effective_cbd
from atlantide.ir.model import IRGraph
from atlantide.policy import PolicyRegistry
from atlantide.reconcile import (
    Change,
    ChangeSet,
    check_prevent_destroy,
    deferred_to_apply,
    diff,
    restrict,
)
from atlantide.reconcile.applied import consumed
from atlantide.reconcile.changes import TypeMutability
from atlantide.reconcile.ordering import behind_destroy_first, resolve_cbd
from atlantide.secrets import (
    SecretsRegistry,
    is_secret_ref_marker,
    secret_ref_from_marker,
)
from atlantide.state import StateGraph


def protected_ids(prior: StateGraph, desired: IRGraph | None = None) -> frozenset[str]:
    """Node ids the ``prevent_destroy`` guard protects in a plan of ``desired`` over ``prior``.

    Terraform's model: for a node the config declares, the flag the config sets
    decides, so protection added in a plan already guards that plan and
    protection removed in it already permits a replace. For a node the config
    does not declare (a DELETE, or ``destroy``, which has no config) the flag
    recorded in state decides, since it is the only record left.
    """
    declared = {node.id: node.prevent_destroy for node in desired.nodes} if desired else {}
    stored = {n.id for n in prior.nodes.values() if n.prevent_destroy and n.id not in declared}
    return frozenset(stored | {node_id for node_id, flag in declared.items() if flag})


def _deferral_notes(deferred: frozenset[str]) -> tuple[str, ...]:
    """The plan warning for each protected node whose replace is judged at apply."""
    return tuple(
        f"{node_id}: prevent_destroy is checked at apply — its replacement is known "
        "only after apply, and the apply refuses it if an immutable value actually changes"
        for node_id in sorted(deferred)
    )


def destroy_changeset(
    state: StateGraph,
    mutability: TypeMutability,
    targets: Sequence[str] = (),
) -> Result[ChangeSet, AtlantideError]:
    """Delete everything in ``state``, or only ``targets`` and their dependents.

    ``prevent_destroy`` is enforced here, like any other plan.
    """
    changes = diff(IRGraph(nodes=()), {}, state, mutability)
    if targets:
        selected = catching(lambda: destroy_selection(state, targets))
        if isinstance(selected, Failure):
            return forward_failure(selected)
        changes = restrict(changes, selected.unwrap())
    return check_prevent_destroy(changes, protected_ids(state))


def raise_drift(approved: ChangeSet, fresh: ChangeSet) -> None:
    """Refuse to execute a changeset that is not the one that was approved.

    Called with the lease held, after the re-diff. State may change between the
    approved plan and the one about to run (another apply landed, or a resource
    was destroyed out of band); a changed diff is refused.
    """
    before, after = approved.fingerprint(), fresh.fingerprint()
    if before == after:
        return
    added = sorted(_drift_entry(entry) for entry in after - before)
    removed = sorted(_drift_entry(entry) for entry in before - after)
    parts = []
    if added:
        parts.append(f"now also: {', '.join(added)}")
    if removed:
        parts.append(f"no longer: {', '.join(removed)}")
    raise PlanDriftError(
        "state changed between the plan you approved and the lock being taken, so "
        "the changes are no longer the ones shown — " + "; ".join(parts) + ". "
        "Re-run to plan against current state."
    )


def _drift_entry(entry: tuple[str, str, tuple[str, ...], bool, bool]) -> str:
    """One :meth:`ChangeSet.fingerprint` entry, with everything it compares.

    The action and node id alone would print a change whose fields or flags
    moved as both added and removed, unchanged.
    """
    node_id, action, changed_fields, conditional, create_before_destroy = entry
    text = f"{action} {node_id}"
    if changed_fields:
        text += f" [{', '.join(changed_fields)}]"
    if conditional:
        text += " (known after apply)"
    if create_before_destroy:
        text += " create-before-destroy"
    return text


def _actionable_fields(changeset: ChangeSet) -> Iterator[tuple[Change, str, Any]]:
    """Every (change, field_name, value) over the actionable nodes' properties.

    A change's fields come from its desired IR node, or its prior state node for a
    pure DELETE. Preserves changeset/property order so callers' sorted diagnostics
    stay stable.
    """
    for change in changeset.actionable:
        node = change.desired or change.prior
        properties = node.properties if node is not None else {}
        for field_name, value in properties.items():
            yield change, field_name, value


class Planner:
    """Turns a compiled config + prior state into a :class:`Plan`.

    Runs the post-diff refinement passes in order (secret-rotation detection,
    :mod:`~atlantide.engine.secret_audit`; undefined-secret and stack-output
    validation; create-before-destroy collision resolution,
    :func:`~atlantide.reconcile.ordering.resolve_cbd`) and then policy
    evaluation (:mod:`~atlantide.engine.policy_eval`), holding their inputs
    (``mutability``/``types``/``secrets``/``policies``).
    """

    def __init__(
        self,
        *,
        mutability: TypeMutability,
        types: dict[str, type[Resource]],
        secrets: SecretsRegistry,
        policies: PolicyRegistry,
    ) -> None:
        self.mutability = mutability
        self.types = types
        self.secrets = secrets
        self.policies = policies

    def build(
        self,
        compiled: Compiled,
        prior: StateGraph,
        stack_outputs: dict[str, Any],
        *,
        selected: frozenset[str] | None = None,
        replace: frozenset[str] = frozenset(),
    ) -> Result[Plan, AtlantideError]:
        """Diff, then shape the result before the policy and safety passes run.

        Forcing a replace and restricting to a selection both happen before
        :func:`check_prevent_destroy`, so a node forced by ``--replace`` is still
        subject to ``prevent_destroy`` and create-before-destroy resolution.

        The diff also sees what each node's ``$ref`` fields resolve to against the
        stored outputs, compared with what they were last applied with (see
        :mod:`atlantide.reconcile.applied`), so a value an interrupted or targeted
        run moved is a known change: a protected node it replaces is refused here.
        """
        # A salted ``$ref`` record cannot be checked without the install key; the
        # SecretsError naming the missing keyfile is the plan's failure.
        moved = catching(lambda: consumed(compiled.ir, prior, self.secrets))
        if isinstance(moved, Failure):
            return forward_failure(moved)
        raw = diff(
            compiled.ir,
            compiled.hashes,
            prior,
            self.mutability,
            replace=replace,
            consumed=moved.unwrap(),
        )
        if selected is not None:
            raw = restrict(raw, selected)
        protected = protected_ids(prior, compiled.ir)
        changeset: Result[ChangeSet, AtlantideError] = check_prevent_destroy(raw, protected)
        notes = _deferral_notes(deferred_to_apply(raw, protected))
        return changeset.bind(lambda cs: self._refine(cs, compiled, prior, stack_outputs, notes))

    def _refine(
        self,
        changeset: ChangeSet,
        compiled: Compiled,
        prior: StateGraph,
        stack_outputs: dict[str, Any],
        notes: tuple[str, ...] = (),
    ) -> Result[Plan, AtlantideError]:
        """Post-diff passes, in order: secrets, then references, then policy.

        The secret audit runs first and once: both of its consumers (upgrading a
        NOOP whose secret rotated, and warning when the mismatches come from a
        foreign keyfile) read the same comparison, which avoids a second round
        trip per secret to a remote store.

        A stored digest with no keyfile to check it against fails the plan (the
        SecretsError names the keyfile path) rather than reading as a rotation.
        """
        audited = catching(lambda: audit_secrets(changeset, prior, self.secrets))
        if isinstance(audited, Failure):
            return forward_failure(audited)
        audit = audited.unwrap()
        return (
            self._require_secrets(
                audit.applied_to(changeset, self.mutability, effective_cbd(compiled.ir))
            )
            .bind(lambda cs: self._require_stack_outputs(cs, stack_outputs))
            .bind(lambda cs: self._finalize(cs, compiled, notes + audit.warnings()))
        )

    def _require_stack_outputs(
        self, changeset: ChangeSet, stack_outputs: dict[str, Any]
    ) -> Result[ChangeSet, AtlantideError]:
        """Fail the plan when a node references a stack output not yet committed."""
        missing = [
            f"{change.node_id}.{field_name} -> {value[STACK_OUTPUT_KEY]!r}"
            for change, field_name, value in _actionable_fields(changeset)
            if is_stack_output_marker(value) and value[STACK_OUTPUT_KEY] not in stack_outputs
        ]
        if missing:
            joined = "; ".join(sorted(missing))
            return Failure(
                AtlantideError(
                    f"undefined stack output(s) — apply the source stack first: {joined}"
                )
            )
        return Success(changeset)

    def _require_secrets(self, changeset: ChangeSet) -> Result[ChangeSet, AtlantideError]:
        """Fail the plan when an actionable node references an undefined secret.

        Every CREATE/UPDATE/REPLACE (from the desired IR) and DELETE (from state)
        resolves its secret handles at apply; check they exist up front so a
        missing secret aborts the plan instead of a half-finished apply.
        """
        missing: list[str] = []
        for change, field_name, value in _actionable_fields(changeset):
            if not is_secret_ref_marker(value):
                continue
            ref = secret_ref_from_marker(value)
            try:
                self.secrets.resolve(ref)
            except SecretsError as exc:
                # The provider's reason (not found, not allow-listed, unreachable)
                # is included; providers never put secret values in it.
                missing.append(f"{change.node_id}.{field_name} -> {ref.name!r} ({exc})")
        if missing:
            return Failure(SecretsError("undefined secret(s): " + "; ".join(sorted(missing))))
        return Success(changeset)

    def _finalize(
        self, changeset: ChangeSet, compiled: Compiled, notes: tuple[str, ...] = ()
    ) -> Result[Plan, AtlantideError]:
        resolved = resolve_cbd(
            changeset,
            types=self.types,
            mutability=self.mutability,
            forcers=cbd_forcers(compiled.ir),
        )
        if isinstance(resolved, Failure):
            return forward_failure(resolved)
        # A downgrade makes an upstream destroy-first: its conditional
        # dependents must go before its delete, as the diff orders them.
        cs, warnings = resolved.unwrap()
        settled = behind_destroy_first(cs, self.mutability)
        # Policy provider errors cross back to Result here.
        return catching(
            lambda: evaluate_policies(settled, compiled, types=self.types, policies=self.policies)
        ).map(
            lambda violations: Plan(
                changeset=settled,
                compiled=compiled,
                violations=violations,
                warnings=notes + warnings,
            )
        )
