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

    ``depends_on`` (explicit ordering edges) is kept out for the same reason, and
    it matters more here: :func:`~atlantide.ir.merkle.merkle_hashes` folds each
    dependency's hash into the dependent's, so an ordering *hint* inside the
    hashed payload would re-hash the node and every node below it — an UPDATE on
    resources whose configuration did not change. Ordering is not identity.
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
    #: Unlike ``aliases``/``depends_on`` this *is* part of identity: the same query
    #: as a data source and as a managed resource are different things, so
    #: converting one to the other must not read as an in-place update.
    kind: str = "resource"

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

        :meth:`to_canonical` is the hashed shape and omits ``aliases`` by design.
        An artifact stores the whole node: without the rename directive a deploy
        lowers a rename to a destroy plus a create.
        """
        node = self.to_canonical()
        if self.aliases:
            node["aliases"] = list(self.aliases)
        if self.depends_on:
            node["depends_on"] = list(self.depends_on)
        return node

    @classmethod
    def from_stored(cls, node: dict[str, Any]) -> IRNode:
        """Rebuild a node from :meth:`to_stored`'s output.

        Deliberately next to its inverse: a field added above and not here is a
        field an artifact records and a deploy silently drops, with nothing to
        fail on.
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
            kind=str(node.get("kind", "resource")),
        )

    def edges(self) -> tuple[str, ...]:
        """Every node that must act before this one: value refs plus explicit ones.

        The two are stored apart because only the first is part of the hash, but
        the scheduler and the diff care about the union — an ordering edge that
        did not order anything would be a lie.
        """
        return tuple(sorted({*self.dependencies, *self.depends_on}))


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
    def from_stored(cls, data: dict[str, Any]) -> IRGraph:
        """Rebuild a graph from :meth:`to_stored`'s output."""
        return cls(
            nodes=tuple(IRNode.from_stored(node) for node in data["nodes"]),
            version=data.get("version", IR_VERSION),
        )

    def node(self, node_id: str) -> IRNode | None:
        """Return the node with ``node_id``, or ``None`` if absent."""
        return next((node for node in self.nodes if node.id == node_id), None)

    def __len__(self) -> int:
        return len(self.nodes)
