"""Secret-rotation audit: live secret values against the digests stored in state.

The IR holds secret handles, not values, so a rotated secret is invisible to
the Merkle diff. The planner audits every unchanged node once
(:func:`audit_secrets`); the resulting :class:`SecretAudit` both upgrades the
rotated NOOPs to re-applies and warns when the misses point at a foreign
keyfile rather than a rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from atlantide.core import AtlantideError
from atlantide.core.actions import Action
from atlantide.core.fields import Mutability
from atlantide.core.node_id import field_scope
from atlantide.reconcile import Change, ChangeSet
from atlantide.reconcile.changes import TypeMutability
from atlantide.reconcile.ordering import behind_destroy_first
from atlantide.secrets import SecretsRegistry, is_secret_ref_marker, secret_ref_from_marker
from atlantide.state import StateGraph, StateNode


@dataclass(frozen=True, slots=True)
class SecretAudit:
    """How the unchanged nodes' live secret values compare to their stored digests.

    One comparison yields both the fields to re-apply and whether the misses are
    rotations.
    """

    #: node id -> field names whose value does not match the stored digest.
    rotated: dict[str, tuple[str, ...]]
    #: Number of digests that verified; any match means this install's salt is correct.
    matched: int
    #: Nodes with a stored digest that did not match; a missing digest is not counted.
    mismatched: frozenset[str]

    @property
    def looks_like_a_foreign_keyfile(self) -> bool:
        """Whether the misses are better explained by the wrong ``secrets_key``.

        Rotation digests are salted per install, so an install without the shared
        keyfile computes every digest under a different salt and every secret reads
        as rotated. Mismatches on two or more resources with no digest matching
        indicate a salt mismatch rather than a rotation.
        """
        return self.matched == 0 and len(self.mismatched) >= 2

    def applied_to(
        self, changeset: ChangeSet, mutability: TypeMutability, cbd: frozenset[str]
    ) -> ChangeSet:
        """Upgrade each NOOP whose secret rotated to a re-apply of those fields.

        The IR is value-independent, so a rotation is invisible to the Merkle
        diff; this pass adds it to the plan. Classified through the same
        mutability the diff uses: a rotated ``immutable()`` field cannot be
        pushed through ``update()``, so it is a REPLACE, not an UPDATE, and
        create-before-destroy when its id is in ``cbd`` (the IR's
        :func:`~atlantide.graph.cbd.effective_cbd`), as the diff would make it.
        A destroy-first one gets the diff's ordering for its conditional
        dependents too (:func:`~atlantide.reconcile.ordering.behind_destroy_first`).
        """
        if not self.rotated:
            return changeset
        upgraded = changeset.map(
            lambda change: (
                self._upgraded(change, fields, mutability, cbd)
                if (fields := self.rotated.get(change.node_id))
                else change
            )
        )
        return behind_destroy_first(upgraded, mutability)

    @staticmethod
    def _upgraded(
        change: Change, fields: tuple[str, ...], mutability: TypeMutability, cbd: frozenset[str]
    ) -> Change:
        assert change.desired is not None  # rotation is only audited on NOOPs with IR
        muts = mutability.get(change.desired.type, {})
        # The upgraded change's own state write records the flag, so it is no
        # longer state-only.
        if any(muts.get(f) is Mutability.IMMUTABLE for f in fields):
            return replace(
                change,
                action=Action.REPLACE,
                changed_fields=fields,
                create_before_destroy=change.node_id in cbd,
                state_only=False,
            )
        return replace(change, action=Action.UPDATE, changed_fields=fields, state_only=False)

    def warnings(self) -> tuple[str, ...]:
        """The non-blocking note to show above the plan, if there is one."""
        if not self.looks_like_a_foreign_keyfile:
            return ()
        return (
            f"every secret in state reads as rotated ({len(self.mismatched)} resources) — "
            f"if they did not all change, this install's secrets_key differs from the "
            f"one that wrote this state; point secrets_key at the shared keyfile "
            f"rather than applying these updates",
        )


def audit_secrets(changeset: ChangeSet, prior: StateGraph, secrets: SecretsRegistry) -> SecretAudit:
    """Compare every unchanged node's secret handles against its stored digests.

    Only NOOPs are audited: a node the Merkle diff already flagged is being
    re-applied regardless, and a rotation is invisible to that diff because the
    IR holds handles rather than values.

    Best-effort: a handle this install cannot resolve is left to apply, which
    resolves it anyway.
    """
    rotated: dict[str, tuple[str, ...]] = {}
    matched = 0
    mismatched: set[str] = set()
    for change in changeset.changes:
        prior_node = prior.get(change.node_id)
        if change.action is not Action.NOOP or change.desired is None or prior_node is None:
            continue
        fields, node_matched, node_mismatched = _audit_node(change, prior_node, secrets)
        matched += node_matched
        if node_mismatched:
            mismatched.add(change.node_id)
        if fields:
            rotated[change.node_id] = fields
    return SecretAudit(rotated=rotated, matched=matched, mismatched=frozenset(mismatched))


def _audit_node(
    change: Change, prior_node: StateNode, secrets: SecretsRegistry
) -> tuple[tuple[str, ...], int, bool]:
    """One NOOP's ``(rotated fields, digests matched, any stored digest missed)``."""
    assert change.desired is not None  # only NOOPs with desired IR are audited
    fields: list[str] = []
    matched = 0
    mismatched = False
    for field_name, value in change.desired.properties.items():
        if not is_secret_ref_marker(value):
            continue
        try:
            plaintext = secrets.resolve(secret_ref_from_marker(value))
        except AtlantideError:
            continue
        stored = prior_node.secret_digests.get(field_name)
        scope = field_scope(change.node_id, field_name)
        if secrets.digest_matches(scope, plaintext, stored):
            matched += 1
            continue
        fields.append(field_name)
        # Only a stored digest that misses indicates a salt mismatch; a
        # missing one (a pre-secrets state row, or a non-sensitive field)
        # does not.
        if stored is not None:
            mismatched = True
    return tuple(sorted(fields)), matched, mismatched
