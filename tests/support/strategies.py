"""Hypothesis generators for atlantide's pure layers.

The lower half of the package is algebraic: total functions over values, with no
clock, no I/O and no configuration. That is the shape generated input is good at,
and these are the inputs those functions take.

Kept in one module so a strategy is written once. ``json_values`` in particular
was inline in ``tests/ir/test_canonical.py`` and is wanted by the codec suite too;
two copies would drift, and a strategy that has drifted quietly tests less than
it looks like it tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, NamedTuple

from hypothesis import strategies as st

from atlantide.core.fields import Mutability
from atlantide.core.types import SEALED_KEY, Ref, SecretRef, Transform
from atlantide.graph.build import build_graph
from atlantide.graph.order import topological_order
from atlantide.ir.merkle import merkle_hashes
from atlantide.ir.model import IRGraph, IRNode
from atlantide.state.backend import (
    NO_INPUT_HASH,
    STATUS_CREATED,
    STATUS_CREATING,
    StateGraph,
    StateNode,
)
from atlantide.state.codec import StateDocument
from tests.support.resources import NestedValue

#: Identifiers that survive a round trip through every layer. Deliberately not
#: `st.text()`: node ids are structured (`stack:type:name`) and a generated colon
#: would make `id.split(":")` mean something it does not.
names = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=12)


def json_values(max_leaves: int = 20) -> st.SearchStrategy[Any]:
    """Anything the canonical encoder accepts: scalars, lists, str-keyed dicts.

    No floats. They are encodable, but `repr` round-tripping and the NaN/inf
    rejection are their own property, asserted separately rather than folded in
    here where a shrink would land on `0.1 + 0.2` and explain nothing.
    """
    return st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=8), children, max_size=4)
        ),
        max_leaves=max_leaves,
    )


def refs() -> st.SearchStrategy[Ref]:
    """A live `Ref` handle — the thing tree walkers exist to find."""
    return st.builds(Ref, node_id=names, attr=names)


def _containers(
    children: st.SearchStrategy[Any], hashable: st.SearchStrategy[Any]
) -> st.SearchStrategy[Any]:
    """Every container the tree walkers treat as structure.

    Written once because both tree generators need the same list for the same
    reason, and a container added to one and not the other would leave the second
    generator quietly testing less than it looks like it tests — which is the
    drift this module exists to prevent.

    The three that matter are the ones a hand-rolled walker stops at: a `set`
    (lowered to a sorted list, because iteration order varies with
    `PYTHONHASHSEED`), a `Transform` (which hides its children behind
    `_atlas_operands`), and a nested pydantic model (which hides them behind
    pydantic's own machinery). ``hashable`` is a separate strategy because a set's
    elements have to be hashable and the recursive `children` are not.
    """
    return (
        st.lists(children, max_size=3)
        | st.tuples(children, children)
        | st.dictionaries(names, children, max_size=3)
        | st.frozensets(hashable, max_size=3)
        | st.builds(lambda *parts: Transform("concat", list(parts)), children, children)
        | st.builds(NestedValue, label=names, value=children)
    )


def property_trees(max_leaves: int = 12) -> st.SearchStrategy[Any]:
    """A resource-input tree: containers, scalars, and live handles."""
    leaves = st.none() | st.booleans() | st.integers() | st.text(max_size=8) | refs()
    return st.recursive(
        leaves,
        lambda children: _containers(children, st.integers() | st.text(max_size=4)),
        max_leaves=max_leaves,
    )


def state_nodes(node_id: str | None = None) -> st.SearchStrategy[StateNode]:
    """A persisted resource row."""
    return st.builds(
        StateNode,
        id=st.just(node_id) if node_id is not None else names,
        type=names,
        provider=names,
        provider_version=st.just("1.0.0"),
        input_hash=names,
        outputs=st.dictionaries(names, json_values(max_leaves=4), max_size=3),
        properties=st.dictionaries(names, json_values(max_leaves=4), max_size=3),
        dependencies=st.lists(names, max_size=3).map(tuple),
        prevent_destroy=st.booleans(),
        secret_digests=st.dictionaries(names, names, max_size=2),
    )


def state_documents() -> st.SearchStrategy[StateDocument]:
    """A whole committed state: nodes, exported outputs, and the serial."""
    return st.builds(
        StateDocument,
        serial=st.integers(min_value=0, max_value=2**31),
        nodes=st.lists(state_nodes(), max_size=4).map(lambda ns: {n.id: n for n in ns}),
        outputs=st.dictionaries(names, json_values(max_leaves=4), max_size=3),
    )


@st.composite
def ir_graphs(
    draw: st.DrawFn, min_nodes: int = 1, max_nodes: int = 6, *, connected: bool = False
) -> IRGraph:
    """A small acyclic IR graph.

    Acyclicity is built in rather than filtered for: a node may only depend on
    one earlier in the list, which is the same shape a real lowering produces and
    avoids throwing away most generated examples.

    ``connected`` gives every node after the first at least one dependency. The
    default may draw a graph with no edges at all, which is the right default —
    an edgeless config is a real one — but leaves a caller that needs an edge to
    work with either filtering or patching one in afterwards.
    """
    count = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    ids = [f"s:t.T:{index}" for index in range(count)]
    nodes = []
    for index, node_id in enumerate(ids):
        dependencies = draw(
            st.lists(
                st.sampled_from(ids[:index]),
                min_size=1 if connected else 0,
                max_size=min(index, 2),
                unique=True,
            )
            if index
            else st.just([])
        )
        nodes.append(
            IRNode(
                id=node_id,
                type="t.T",
                provider="t",
                provider_version="1.0.0",
                properties=draw(st.dictionaries(names, json_values(max_leaves=4), max_size=3)),
                dependencies=tuple(dependencies),
            )
        )
    return IRGraph(nodes=tuple(nodes))


class PlantedCycle(NamedTuple):
    """A cyclic graph and the back edge that made it one."""

    ir: IRGraph
    dependency: str
    dependent: str


@st.composite
def cyclic_ir_graphs(draw: st.DrawFn) -> PlantedCycle:
    """A graph with one back edge added, and the two ids that edge joins.

    Separate from :func:`ir_graphs` because that one builds acyclicity in and so
    can never produce the input :func:`~atlantide.graph.build.build_graph` exists
    to refuse.

    The edge runs from one of a node's *transitive* dependencies back to the node
    itself, which is what makes the result cyclic by construction rather than by
    luck. Adding a merely forward edge — from an earlier id to a later one — closes
    a loop only when the later node already reaches back, so a graph built that way
    is usually still acyclic and a test over it passes while asserting nothing.

    Naming the joined ids lets a test check that the reported cycle is the one
    that was planted, rather than only that *something* was reported.
    """
    ir = draw(ir_graphs(min_nodes=2, connected=True))
    by_id = {node.id: node for node in ir.nodes}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(by_id[start].dependencies)
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(by_id[current].dependencies)
        return seen

    # `connected=True` guarantees every node but the first has a dependency, so
    # there is always something to bend back and no empty-candidates case.
    reach = {node.id: reachable(node.id) for node in ir.nodes}
    dependent = draw(st.sampled_from(sorted(nid for nid, seen in reach.items() if seen)))
    dependency = draw(st.sampled_from(sorted(reach[dependent])))
    return PlantedCycle(
        ir=IRGraph(
            nodes=tuple(
                replace(node, dependencies=(*node.dependencies, dependent))
                if node.id == dependency
                else node
                for node in ir.nodes
            )
        ),
        dependency=dependency,
        dependent=dependent,
    )


def hashes_for(ir: IRGraph) -> dict[str, str]:
    """The Merkle hashes of ``ir``, derived the way the engine derives them.

    Every property that diffs a generated graph needs these, and spelling the
    three-call chain out at each site invites one of them to get it subtly wrong —
    hashing in declaration order rather than dependency order still returns a
    dict, just not the one the diff compares against.
    """
    return merkle_hashes(ir, topological_order(build_graph(ir).unwrap()))


def applied_row(node: IRNode, hashes: Mapping[str, str]) -> StateNode:
    """The state row a successful apply of ``node`` leaves behind.

    A hand copy of :meth:`~atlantide.reconcile.executor.Executor._state_node`,
    restricted to the fields the diff can observe. ``outputs`` and
    ``secret_digests`` are empty because they come from the provider and the
    keyfile, and no branch of :func:`~atlantide.reconcile.diff.diff` reads either —
    ``_change_for`` looks at ``status`` and ``input_hash``, ``_changed_fields`` at
    ``properties`` and ``input_hash``.

    That restriction is what lets the idempotency property run without an
    executor, a provider, or an event loop. It is also what makes the model a
    liability if the executor's row ever diverges, so a pin test in
    ``tests/reconcile/test_diff_properties.py`` compares this against a real apply.
    """
    return StateNode(
        id=node.id,
        type=node.type,
        provider=node.provider,
        provider_version=node.provider_version,
        input_hash=hashes[node.id],
        outputs={},
        properties=node.properties,
        dependencies=node.dependencies,
        prevent_destroy=node.prevent_destroy,
        status=STATUS_CREATED,
        secret_digests={},
    )


def mutabilities(ir: IRGraph) -> st.SearchStrategy[dict[str, dict[str, Mutability]]]:
    """Per-type field mutability: the third argument the diff takes.

    Keyed on the types and field names *actually present* in ``ir``, so an
    IMMUTABLE draw can land on a field that changed and produce a REPLACE — the
    branch worth generating. A map over invented names would type-check, satisfy
    the signature, and make every example an UPDATE.
    """
    types = sorted({node.type for node in ir.nodes})
    fields = sorted({key for node in ir.nodes for key in node.properties})
    if not types or not fields:
        return st.just({})
    return st.dictionaries(
        st.sampled_from(types),
        st.dictionaries(st.sampled_from(fields), st.sampled_from(list(Mutability)), max_size=3),
        max_size=3,
    )


#: How an applied row can have been left behind, beyond the clean case. Keyed by
#: the name a shrink report shows, so a failure says "poisoned" rather than
#: naming a lambda. ``absent`` is the fifth condition and needs no builder — it is
#: the row not being there at all.
_ROW_CONDITIONS: dict[str, Callable[[StateNode], StateNode]] = {
    "applied": lambda row: row,
    "poisoned": lambda row: replace(row, input_hash=NO_INPUT_HASH),
    "creating": lambda row: replace(row, status=STATUS_CREATING),
    "drifted": lambda row: replace(row, input_hash="0" * 64, properties={"drifted": True}),
}


@st.composite
def prior_states(
    draw: st.DrawFn, ir: IRGraph, hashes: Mapping[str, str], *, orphans: int = 2
) -> StateGraph:
    """A state file to diff against, drawn from the conditions a real one reaches.

    Not "the state after a clean apply" — that would make the idempotency property
    a restatement of the Merkle skip, since the diff NOOPs on hash equality and
    the model is what set the hash. Each node lands in one of the five conditions
    in :data:`_ROW_CONDITIONS` instead, and the interesting ones are the two the
    hash cannot express: a row poisoned by ``refresh --write`` (which no digest
    equals) and a row left ``creating`` by an apply that died between the
    write-ahead and the confirm.

    ``orphans`` adds rows for ids the config no longer declares. Those are the
    DELETEs, and without them the property never exercises removal.
    """
    nodes: dict[str, StateNode] = {}
    desired_ids = {node.id for node in ir.nodes}
    for node in ir.nodes:
        condition = draw(st.sampled_from(["absent", *_ROW_CONDITIONS]))
        if condition != "absent":
            nodes[node.id] = _ROW_CONDITIONS[condition](applied_row(node, hashes))
    for orphan in draw(st.lists(state_nodes(), max_size=orphans)):
        if orphan.id not in desired_ids:
            nodes[orphan.id] = orphan
    return StateGraph(nodes=nodes)


#: The plaintext planted inside generated secret markers. Distinctive so that
#: scanning redacted output for it cannot match by accident, and deliberately
#: never emitted as a bare leaf — a sentinel that could appear outside a marker
#: would make "no plaintext survives" false by construction rather than by bug.
SECRET_SENTINEL = "PLAINTEXT-MUST-NEVER-BE-LOGGED"


def secret_marker_trees(max_leaves: int = 12) -> st.SearchStrategy[Any]:
    """A log payload with secret markers buried at arbitrary depth.

    Uses the same containers as :func:`property_trees` — every kind the walkers
    treat as structure, not just the dicts and lists a payload obviously has —
    because a walker that stops at any of them logs the value it exists to hide.

    The ``$sealed`` marker appears both alone and alongside a second key, because
    redaction tests key *membership* — a one-key dict would not catch a check
    written as an equality against the marker's exact shape.

    Leaves are drawn from an alphabet with no ``$`` and no sentinel, so a scan of
    the output cannot be satisfied by a plain string that happened to spell one.
    """
    markers = st.sampled_from(
        [
            SecretRef(SECRET_SENTINEL).canonical(),
            {SEALED_KEY: SECRET_SENTINEL},
            {SEALED_KEY: SECRET_SENTINEL, "algorithm": "aes-256-gcm"},
        ]
    )
    plain = st.text(alphabet="abcdefghij", max_size=6)
    return st.recursive(
        plain | st.integers() | st.none() | markers,
        lambda children: _containers(children, plain),
        max_leaves=max_leaves,
    )
