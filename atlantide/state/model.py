"""The storage-independent state model: nodes and the graph they form."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "NO_INPUT_HASH",
    "NodeStatus",
    "StateGraph",
    "StateNode",
]


class NodeStatus(StrEnum):
    """Where a node's create stands. A ``str``, so stored bytes are the plain value."""

    #: A node fully created and confirmed (outputs recorded).
    CREATED = "created"
    #: A write-ahead row: a create was started but not confirmed. Re-created on
    #: the next plan, and reclaimable by destroy/refresh even if the create leaked.
    CREATING = "creating"


#: ``input_hash`` for a node whose live inputs have drifted from config. No sha256
#: digest equals it, so the diff's Merkle skip cannot fire and the node is
#: re-planned. Written by ``refresh --write``, which is how a provider read
#: reaches the next plan: a symbolic diff cannot detect drift.
NO_INPUT_HASH = ""


@dataclass(frozen=True, slots=True)
class StateNode:
    """A single persisted resource: desired inputs' hash + realised outputs."""

    id: str
    type: str
    provider: str
    provider_version: str
    input_hash: str
    outputs: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    prevent_destroy: bool = False
    status: str = NodeStatus.CREATED
    #: field name -> hex digest of the last-resolved secret value, for rotation
    #: detection. ``properties`` carries only the ``{"$secret_ref": ...}`` handle;
    #: the value itself is never stored.
    secret_digests: dict[str, str] = field(default_factory=dict)
    #: field name -> digest of the value that field's ``$ref`` markers resolved to
    #: when this row was written, for every ``$ref``-bearing property. ``properties``
    #: keep the markers, which cannot say *which* upstream value the resource was
    #: given; this does, so a plan can tell that an upstream output the node
    #: consumes moved since it was last applied. Digests only, never values: see
    #: :mod:`atlantide.reconcile.applied`. Empty on a row written before it existed,
    #: which the diff treats as "unknown".
    ref_digests: dict[str, str] = field(default_factory=dict)
    #: Ordering-only edges declared with ``depends_on=``, kept apart from
    #: ``dependencies`` as in the IR (they are not part of the hash). Recorded so
    #: a destroy, whose graph comes from state alone, still honours them. Empty on
    #: a row written before it existed.
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StateGraph:
    """The committed state as an id-keyed set of nodes."""

    nodes: dict[str, StateNode] = field(default_factory=dict)

    def get(self, node_id: str) -> StateNode | None:
        return self.nodes.get(node_id)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)
