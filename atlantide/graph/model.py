"""The dependency graph: a directed acyclic graph of resource node ids.

Edges run from a dependency to its dependent; both directions are precomputed
so forward (create/update) and reverse (destroy) traversal are O(1) lookups.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiGraph:
    """Immutable resource DAG.

    - ``deps[n]``       : node ids that ``n`` depends on (must act before ``n``).
    - ``dependents[n]`` : node ids that depend on ``n`` (must act after ``n``).
    Both value tuples are sorted for deterministic traversal.
    """

    node_ids: tuple[str, ...]
    deps: dict[str, tuple[str, ...]]
    dependents: dict[str, tuple[str, ...]]

    def __len__(self) -> int:
        return len(self.node_ids)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.deps

    def predecessors(self, node_id: str, *, reverse: bool) -> tuple[str, ...]:
        """Nodes that must act before ``node_id`` for the given direction."""
        return self.dependents[node_id] if reverse else self.deps[node_id]

    def successors(self, node_id: str, *, reverse: bool) -> tuple[str, ...]:
        """Nodes unblocked once ``node_id`` has acted, for the given direction."""
        return self.deps[node_id] if reverse else self.dependents[node_id]
