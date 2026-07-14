"""Atlas IR: the canonical, language-independent form of a config.

An :class:`IRGraph` is a sorted list of :class:`IRNode`s. It is what every
downstream stage (graph build, diff, planner, executor) consumes — never the
live Python objects. Its canonical JSON encoding is the plan identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IR_VERSION = 1


@dataclass(frozen=True, slots=True)
class IRNode:
    """One resource, flattened to serializable data.

    Lifecycle flags (``prevent_destroy``/``create_before_destroy``/
    ``ignore_changes``) are declarative, so they travel in the IR and survive to
    a source-less deploy. They only enter the canonical (hashed) form when set —
    a lifecycle-free config lowers to byte-identical IR.

    ``aliases`` (prior ids this node was renamed from) is a *migration directive*,
    not part of the resource's identity — it is deliberately kept out of
    ``to_canonical`` so adding/removing an alias never moves the content hash.
    """

    id: str
    type: str
    provider: str
    provider_version: str
    properties: dict[str, Any]
    dependencies: tuple[str, ...]
    prevent_destroy: bool = False
    create_before_destroy: bool = False
    ignore_changes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def to_canonical(self) -> dict[str, Any]:
        node: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "properties": self.properties,
            "dependencies": list(self.dependencies),
        }
        if self.prevent_destroy:
            node["prevent_destroy"] = True
        if self.create_before_destroy:
            node["create_before_destroy"] = True
        if self.ignore_changes:
            node["ignore_changes"] = list(self.ignore_changes)
        return node


@dataclass(frozen=True, slots=True)
class IRGraph:
    """The whole config as IR. ``nodes`` are sorted by id."""

    nodes: tuple[IRNode, ...]
    version: int = IR_VERSION

    def to_canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [node.to_canonical() for node in self.nodes],
        }

    def node(self, node_id: str) -> IRNode | None:
        """Return the node with ``node_id``, or ``None`` if absent."""
        return next((node for node in self.nodes if node.id == node_id), None)

    def __len__(self) -> int:
        return len(self.nodes)
