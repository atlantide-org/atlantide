"""Merkle ``input_hash`` over the Atlas IR.

Each node's hash folds in its type, its canonical properties (with symbolic
``$ref`` markers), and the hashes of its dependencies:

    h(n) = sha256( canonical({type, properties, [h(dep) for dep in sorted deps]}) )

Including dependency hashes makes any upstream change ripple into every
dependent's hash. The diff engine compares it to the stored ``input_hash`` for
NOOP-skipping (equal hash => unchanged subtree, no provider ``read``).

The properties keep symbolic ``$ref`` markers, so the hash needs no provider I/O
and equals the value the executor persists after apply. A ref's resolved value
changes only if the referenced node changed, and that change is already folded in
via the dependency hashes. So an unchanged hash means the resolved inputs are
unchanged; no apply-time recompute is required.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from atlantide.ir.canonical import to_canonical_json
from atlantide.ir.model import IRGraph


def merkle_hashes(ir: IRGraph, topo_order: Sequence[str]) -> dict[str, str]:
    """Return node id -> Merkle input_hash, computed in dependency order."""
    by_id = {node.id: node for node in ir.nodes}
    hashes: dict[str, str] = {}
    for node_id in topo_order:
        node = by_id[node_id]
        # ``ignore_changes`` fields are excluded so drift in them never moves the
        # hash, and so never triggers an UPDATE or REPLACE. The diff drops them too.
        ignored = set(node.ignore_changes)
        properties = (
            {k: v for k, v in node.properties.items() if k not in ignored}
            if ignored
            else node.properties
        )
        payload = {
            "type": node.type,
            "properties": properties,
            "deps": [hashes[dep] for dep in sorted(node.dependencies)],
        }
        hashes[node_id] = hashlib.sha256(to_canonical_json(payload)).hexdigest()
    return hashes
