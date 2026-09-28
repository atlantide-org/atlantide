"""Atlas IR: the canonical, language-independent form of a config.

An :class:`IRGraph` is a sorted list of :class:`IRNode`s. Every downstream stage
(graph build, diff, planner, executor) consumes it, not the live Python objects.
Its canonical JSON encoding is the plan identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self, cast

IR_VERSION = 1

#: What an IR node stands for: a managed ``"resource"`` or a read-only ``"data"`` lookup.
type NodeKind = Literal["resource", "data"]
_NODE_KINDS: frozenset[str] = frozenset({"resource", "data"})


@dataclass(frozen=True, slots=True)
class IRNode:
    """One resource, flattened to serializable data.

    Lifecycle flags (``prevent_destroy``/``create_before_destroy``/
    ``ignore_changes``) are declarative, so they travel in the IR and survive to
    a source-less deploy. They enter the canonical (hashed) form only when set;
    unset flags add no keys.

    ``aliases`` (prior ids this node was renamed from) is a migration directive,
    not part of the resource's identity. It is kept out of ``to_canonical`` so
    adding or removing an alias does not change the content hash.

    ``depends_on`` (explicit ordering edges) is kept out for the same reason:
    :func:`~atlantide.ir.merkle.merkle_hashes` folds each dependency's hash into
    the dependent's, so an ordering edge in the hashed payload would change the
    hash of the node and all its dependents and plan UPDATEs on unchanged
    resources.
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
    #: Ordering-only edges declared with ``depends_on=``; see the class docstring.
    depends_on: tuple[str, ...] = ()
    #: ``"data"`` for a read-only lookup, ``"resource"`` for something managed.
    #: Part of identity, unlike ``aliases`` and ``depends_on``: converting between
    #: a data source and a managed resource must not plan as an in-place update.
    kind: NodeKind = "resource"

    @property
    def is_data(self) -> bool:
        """Whether this node is a data source: read, never created or destroyed."""
        return self.kind == "data"

    def to_canonical(self) -> dict[str, Any]:
        node: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "properties": self.properties,
            "dependencies": list(self.dependencies),
        }
        if self.kind != "resource":
            node["kind"] = self.kind
        if self.prevent_destroy:
            node["prevent_destroy"] = True
        if self.create_before_destroy:
            node["create_before_destroy"] = True
        if self.ignore_changes:
            node["ignore_changes"] = list(self.ignore_changes)
        return node

    def to_stored(self) -> dict[str, Any]:
        """The full serialized form, including fields the hash leaves out.

        :meth:`to_canonical` is the hashed shape and omits ``aliases`` and
        ``depends_on``. An artifact stores the whole node: without the rename
        directive a deploy plans a rename as a destroy plus a create.
        """
        node = self.to_canonical()
        if self.aliases:
            node["aliases"] = list(self.aliases)
        if self.depends_on:
            node["depends_on"] = list(self.depends_on)
        return node

    @classmethod
    def from_stored(cls, node: dict[str, Any]) -> Self:
        """Rebuild a node from :meth:`to_stored`'s output.

        Must stay in sync with :meth:`to_stored`: a field written there but not
        read here is dropped on deploy without an error.
        """
        return cls(
            id=node["id"],
            type=node["type"],
            provider=node["provider"],
            provider_version=node["provider_version"],
            properties=node["properties"],
            dependencies=tuple(node["dependencies"]),
            prevent_destroy=node.get("prevent_destroy", False),
            create_before_destroy=node.get("create_before_destroy", False),
            ignore_changes=tuple(node.get("ignore_changes", ())),
            aliases=tuple(node.get("aliases", ())),
            depends_on=tuple(node.get("depends_on", ())),
            kind=_node_kind(node.get("kind", "resource")),
        )

    def edges(self) -> tuple[str, ...]:
        """Every node that must act before this one: value refs plus explicit ones.

        The two are stored apart because only the first is part of the hash; the
        scheduler and the diff use the union.
        """
        return tuple(sorted({*self.dependencies, *self.depends_on}))


def _node_kind(value: Any) -> NodeKind:
    """``value`` as a :data:`NodeKind`, or ``ValueError`` for anything else."""
    kind = str(value)
    if kind not in _NODE_KINDS:
        raise ValueError(f"unknown node kind {kind!r}")
    return cast("NodeKind", kind)


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

    def to_stored(self) -> dict[str, Any]:
        """The serialized form for an artifact; see :meth:`IRNode.to_stored`."""
        return {
            "version": self.version,
            "nodes": [node.to_stored() for node in self.nodes],
        }

    @classmethod
    def from_stored(cls, data: dict[str, Any]) -> Self:
        """Rebuild a graph from :meth:`to_stored`'s output."""
        return cls(
            nodes=tuple(IRNode.from_stored(node) for node in data["nodes"]),
            version=data["version"],
        )

    def node(self, node_id: str) -> IRNode | None:
        """Return the node with ``node_id``, or ``None`` if absent."""
        return next((node for node in self.nodes if node.id == node_id), None)

    def __len__(self) -> int:
        return len(self.nodes)
