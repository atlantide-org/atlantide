"""Did an upstream value move: the plan-side reading of a node's ``$ref`` fields.

Two sources answer it. Symbolically, a field whose marker is unchanged still
resolves to a new value when an upstream it references changes this run
(:func:`touches`). Against the record, :func:`atlantide.reconcile.applied.consumed`
reports a :class:`Consumed` verdict per field; :func:`settled` and
:func:`unrecorded` keep only the verdicts whose upstreams all keep their outputs
this run.

The verdicts are computed in :mod:`~atlantide.reconcile.applied`, which reads
stored outputs; this module only reads them, so the diff stays pure
(import-linter contract "diff and guards are pure").
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from enum import StrEnum
from typing import Any

from atlantide.core.markers import collect_ref_targets
from atlantide.ir.model import IRNode

__all__ = [
    "NO_VERDICTS",
    "Consumed",
    "ConsumedRefs",
    "settled",
    "touches",
    "unrecorded",
]


class Consumed(StrEnum):
    """A stored row's ``$ref`` field, compared with what it resolves to now.

    Computed by :func:`atlantide.reconcile.applied.consumed` against the
    upstreams' stored outputs. Only the two informative verdicts are reported: a
    field whose value is unchanged, or that cannot be resolved, is left out.
    """

    #: The value differs from the one recorded when the node was last applied.
    MOVED = "moved"
    #: The row records nothing for the field: it was written before the record.
    UNRECORDED = "unrecorded"


#: Per node id, the :class:`Consumed` verdict of each reported field.
type ConsumedRefs = Mapping[str, Mapping[str, Consumed]]

#: No verdicts: a node the caller reported nothing for.
NO_VERDICTS: Mapping[str, Consumed] = {}


def touches(desired: IRNode, value: Any, moved: Set[str]) -> bool:
    """Whether ``value`` references an upstream in ``moved``, ``desired`` itself aside.

    ``moved`` is the whole run's set, which holds ``desired``'s own id when it
    changes too; skipping it here spares a per-node copy without it. A real
    self-reference is a cycle, which ``build_graph`` rejects.
    """
    return any(target in moved for target in collect_ref_targets(value) if target != desired.id)


def _stable(desired: IRNode, name: str, moved: Set[str]) -> bool:
    """Whether every upstream ``name`` references keeps its outputs this run."""
    return not touches(desired, desired.properties.get(name), moved)


def settled(desired: IRNode, verdicts: Mapping[str, Consumed], moved: Set[str]) -> frozenset[str]:
    """Fields known to consume a moved value: :attr:`Consumed.MOVED`, upstreams stable.

    Behind an upstream that changes this run, the value the field will get is
    unknown until apply, so the verdict against its *stored* outputs is moot:
    the field is attributed to that upstream instead, and stays conditional.
    """
    return frozenset(
        name
        for name, verdict in verdicts.items()
        if verdict is Consumed.MOVED and _stable(desired, name, moved)
    )


def unrecorded(
    desired: IRNode, verdicts: Mapping[str, Consumed], moved: Set[str]
) -> tuple[str, ...]:
    """Ref fields a pre-record row cannot vouch for, upstreams stable, not ignored."""
    ignored = set(desired.ignore_changes)
    return tuple(
        sorted(
            name
            for name, verdict in verdicts.items()
            if verdict is Consumed.UNRECORDED
            and name not in ignored
            and _stable(desired, name, moved)
        )
    )
