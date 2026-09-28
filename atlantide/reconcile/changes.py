"""The diff's output: :class:`Change`, :class:`ChangeSet`, and what classifies them.

Imported by everything downstream of the diff (the guards, the planner, the
executor, :mod:`atlantide.testing`); it imports only ``core``, ``ir`` and the
state *model*, never storage.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from atlantide.core.actions import Action
from atlantide.core.fields import Mutability, field_mutability
from atlantide.core.resource import Resource
from atlantide.ir.model import IRNode

# From the model module, not `atlantide.state`: the package imports the sqlite
# backend, and the diff must not import storage (import-linter contract "diff and
# guards are pure").
from atlantide.state.model import StateNode

__all__ = [
    "FORCED_FIELD",
    "Change",
    "ChangeSet",
    "TypeMutability",
    "restrict",
    "type_mutability",
]

#: Per resource type, each field's mutability: type name -> field name -> mutability.
type TypeMutability = Mapping[str, Mapping[str, Mutability]]


def type_mutability(types: Mapping[str, type[Resource]]) -> dict[str, dict[str, Mutability]]:
    """The :data:`TypeMutability` table ``diff`` classifies by, for every type in ``types``.

    The one place it is built: the :class:`~atlantide.engine.Engine` and
    :mod:`atlantide.testing` both call it, so a test classifies exactly as a run does.
    """
    return {name: field_mutability(cls) for name, cls in types.items()}


#: The ``changed_fields`` entry of a REPLACE forced by ``--replace`` rather than
#: by a field change. Shown in the plan verbatim; also marks a change the
#: refinement pass must not re-attribute.
FORCED_FIELD = "(forced)"


@dataclass(frozen=True, slots=True)
class Change:
    node_id: str
    action: Action
    desired: IRNode | None = None
    prior: StateNode | None = None
    changed_fields: tuple[str, ...] = ()
    #: A REPLACE whose only immutable changes are ``$ref``-bearing fields, so it is
    #: known after apply: the executor re-diffs it once the refs resolve and runs
    #: it as an UPDATE (or NOOP) if no immutable value actually moved.
    conditional: bool = False
    create_before_destroy: bool = False
    #: A NOOP whose stored ``prevent_destroy`` differs from the config's. The
    #: flag is not hashed, so nothing else moved: the apply rewrites the state row
    #: with the new flag and calls no provider. Always ``False`` on other actions,
    #: whose ordinary state write records the flag anyway.
    state_only: bool = False
    #: The ``changed_fields`` whose config is unchanged but whose ``$ref`` now
    #: resolves, against the upstreams' stored outputs, to a value other than the
    #: one this node was last applied with: an earlier run moved the upstream
    #: and stopped (or was narrowed) before this node. Known now, never
    #: conditional. Shown in the plan; not part of :meth:`ChangeSet.fingerprint`,
    #: which ``changed_fields`` already covers.
    upstream_moved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangeSet:
    changes: tuple[Change, ...]

    def by_action(self, action: Action) -> list[Change]:
        return [c for c in self.changes if c.action is action]

    @property
    def actionable(self) -> list[Change]:
        """The changes that call a provider: every non-NOOP."""
        return [c for c in self.changes if c.action is not Action.NOOP]

    @property
    def pending(self) -> list[Change]:
        """The changes an apply would make: :attr:`actionable` plus state-only NOOPs.

        What ``--detailed-exitcode`` and the apply prompt go by: a state-only
        change calls no provider, but until it is applied, state disagrees with
        the config about what is protected.
        """
        return [c for c in self.changes if c.action is not Action.NOOP or c.state_only]

    def __iter__(self) -> Iterator[Change]:
        return iter(self.changes)

    def map(self, fn: Callable[[Change], Change]) -> ChangeSet:
        """A new ChangeSet with ``fn`` applied to every change (order kept)."""
        return ChangeSet(tuple(fn(change) for change in self.changes))

    def fingerprint(self) -> frozenset[tuple[str, str, tuple[str, ...], bool, bool]]:
        """What this changeset *does*, as a comparable value.

        Covers exactly what a reviewer is shown. Plain NOOPs are excluded (they
        approve nothing), as are the `IRNode`/`StateNode` payloads, because two
        runs of the same config produce equal-but-not-identical objects. A
        state-only NOOP is included: it rewrites a row's ``prevent_destroy``.

        A set rather than a sequence: the plan is a graph, and scheduler order is
        not part of what was approved.
        """
        return frozenset(
            (
                change.node_id,
                change.action.value,
                change.changed_fields,
                change.conditional,
                change.create_before_destroy,
            )
            for change in self.pending
        )


def restrict(changeset: ChangeSet, selected: frozenset[str]) -> ChangeSet:
    """Downgrade every change outside ``selected`` to NOOP.

    ``--target`` filters the *changeset* rather than the IR: lowering a config
    subset changes the remaining nodes' Merkle hashes (each folds in its
    dependencies'), which the apply would persist. A NOOP writes nothing, so every
    untargeted node keeps its stored ``input_hash``.
    """
    return changeset.map(
        lambda change: change if change.node_id in selected else Change(change.node_id, Action.NOOP)
    )
