"""Plan refinement and policy evaluation: compiled config + prior state -> Plan."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError, PolicyBinding, Resource
from atlantide.core.errors import SecretsError
from atlantide.core.fields import Mutability, physical_name_field
from atlantide.core.markers import STACK_OUTPUT_KEY, is_stack_output_marker
from atlantide.core.node_id import field_scope, stack_of
from atlantide.engine.model import Compiled, Plan
from atlantide.policy import PolicyContext, PolicyRegistry, Violation, class_bindings
from atlantide.reconcile import Action, Change, ChangeSet, diff, plan
from atlantide.secrets import (
    SecretsRegistry,
    is_secret_ref_marker,
    secret_ref_from_marker,
)
from atlantide.state import StateGraph


def protected_ids(prior: StateGraph) -> frozenset[str]:
    """Node ids in state whose lifecycle sets ``prevent_destroy``."""
    return frozenset(n.id for n in prior.nodes.values() if n.prevent_destroy)


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

    Owns the post-diff refinement passes — secret-rotation detection, undefined-
    secret validation, create-before-destroy collision resolution — plus policy
    evaluation, holding their inputs (``mutability``/``types``/``secrets``/
    ``policies``).
    """

    def __init__(
        self,
        *,
        mutability: dict[str, dict[str, Mutability]],
        types: dict[str, type[Resource]],
        secrets: SecretsRegistry,
        policies: PolicyRegistry,
    ) -> None:
        self.mutability = mutability
        self.types = types
        self.secrets = secrets
        self.policies = policies

    def build(
        self, built: Compiled, prior: StateGraph, stack_outputs: dict[str, Any]
    ) -> Result[Plan, AtlantideError]:
        changeset: Result[ChangeSet, AtlantideError] = plan(
            diff(built.ir, built.hashes, prior, self.mutability), protected_ids(prior)
        ).map(lambda cs: self._detect_secret_rotation(cs, prior))
        return (
            changeset.bind(self._require_secrets)
            .bind(lambda cs: self._require_stack_outputs(cs, stack_outputs))
            .bind(lambda cs: self._finalize(cs, built))
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
            except SecretsError:
                missing.append(f"{change.node_id}.{field_name} -> {ref.name!r}")
        if missing:
            return Failure(
                SecretsError("undefined secret(s): " + "; ".join(sorted(missing)))
            )
        return Success(changeset)

    def _detect_secret_rotation(self, changeset: ChangeSet, prior: StateGraph) -> ChangeSet:
        """Upgrade a NOOP to UPDATE when a secret rotated (same handle, new value).

        The IR is value-independent, so a rotation is invisible to the Merkle diff.
        This resolves each unchanged node's secret handles from the backend and
        compares digests to state — best-effort: an unresolvable secret at plan is
        left to apply (which requires resolution).
        """
        changes = []
        for change in changeset.changes:
            rotated = (
                self._rotated_fields(change, prior)
                if change.action is Action.NOOP and change.desired is not None
                else ()
            )
            if rotated:
                change = replace(change, action=Action.UPDATE, changed_fields=rotated)
            changes.append(change)
        return ChangeSet(tuple(changes))

    def _rotated_fields(self, change: Change, prior: StateGraph) -> tuple[str, ...]:
        node = change.desired
        prior_node = prior.get(change.node_id)
        if node is None or prior_node is None:
            return ()
        rotated: list[str] = []
        for field_name, value in node.properties.items():
            if not is_secret_ref_marker(value):
                continue
            try:
                plaintext = self.secrets.resolve(secret_ref_from_marker(value))
            except AtlantideError:
                continue  # best-effort at plan; apply is authoritative
            scope = field_scope(change.node_id, field_name)
            stored = prior_node.secret_digests.get(field_name)
            if not self.secrets.digest_matches(scope, plaintext, stored):
                rotated.append(field_name)
        return tuple(sorted(rotated))

    def _finalize(self, changeset: ChangeSet, built: Compiled) -> Result[Plan, AtlantideError]:
        resolved, warnings = self._resolve_cbd(changeset)
        try:
            violations = self._evaluate_policies(resolved, built)
        except AtlantideError as exc:  # policy provider errors cross back to Result here
            return Failure(exc)
        return Success(
            Plan(changeset=resolved, compiled=built, violations=violations, warnings=warnings)
        )

    def _resolve_cbd(self, changeset: ChangeSet) -> tuple[ChangeSet, tuple[str, ...]]:
        """Downgrade create-before-destroy REPLACEs that would collide on identity.

        CBD needs the new resource to coexist with the old; when the replacement
        keeps the old identity (its physical name, or — for types that declare
        none — no immutable field changed), fall back to destroy-before-create.
        """
        resolved = [self._resolve_one_cbd(change) for change in changeset.changes]
        changes = tuple(change for change, _ in resolved)
        warnings = tuple(warning for _, warning in resolved if warning)
        return ChangeSet(changes), warnings

    def _resolve_one_cbd(self, change: Change) -> tuple[Change, str | None]:
        if not self._cbd_collides(change):
            return change, None
        warning = (
            f"{change.node_id}: create_before_destroy not possible "
            "(replacement shares the old identity); using destroy-before-create"
        )
        return replace(change, create_before_destroy=False), warning

    def _cbd_collides(self, change: Change) -> bool:
        """Whether a create-before-destroy REPLACE would clash with the old resource."""
        if not (change.action is Action.REPLACE and change.create_before_destroy):
            return False
        assert change.desired is not None and change.prior is not None
        type_name = change.desired.type
        cls = self.types.get(type_name)
        name_field = physical_name_field(cls) if cls is not None else None
        if name_field is not None:
            # Distinct only when the cloud name itself changes.
            return change.desired.properties.get(name_field) == change.prior.properties.get(
                name_field
            )
        # No declared identity: the replacement is distinct only if some immutable
        # field changed (otherwise it would occupy the same slot as the old).
        mutability = self.mutability.get(type_name, {})
        return not any(
            mutability.get(f) is Mutability.IMMUTABLE for f in change.changed_fields
        )

    def _evaluate_policies(
        self, changeset: ChangeSet, compiled: Compiled
    ) -> tuple[Violation, ...]:
        violations: list[Violation] = []
        for change in changeset.actionable:  # skip NOOP
            node = change.desired or change.prior
            type_name = node.type if node is not None else ""
            ctx = PolicyContext(
                node_id=change.node_id,
                action=change.action,
                stack=stack_of(change.node_id),
                resource=compiled.resources.get(change.node_id),
            )
            for binding in self._bindings_for(type_name, compiled):
                result = self.policies.evaluate(binding.name, ctx)
                if not result.passed:
                    violations.append(
                        Violation(binding.name, binding.level, change.node_id, result.message)
                    )
        return tuple(violations)

    def _bindings_for(self, type_name: str, compiled: Compiled) -> list[PolicyBinding]:
        config_bindings = [b for b in compiled.policy_bindings if b.applies_to(type_name)]
        cls = self.types.get(type_name)
        decorated = list(class_bindings(cls)) if cls is not None else []
        return config_bindings + decorated
