"""Merkle input_hash: stability, change detection, dependency propagation."""

from __future__ import annotations

from atlantide.graph import build_graph, topological_order
from atlantide.ir import lower, merkle_hashes
from atlantide.lang import evaluate_source
from tests.support import Box, globals_of


def _hashes(source: str) -> dict[str, str]:
    reg = evaluate_source(source, extra_globals=globals_of(Box)).unwrap()
    ir = lower(reg)
    order = topological_order(build_graph(ir).unwrap())
    return merkle_hashes(ir, order)


CFG = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"


def test_hashes_are_stable() -> None:
    assert _hashes(CFG) == _hashes(CFG)


def test_hash_is_hex_sha256() -> None:
    for h in _hashes(CFG).values():
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_leaf_change_is_isolated() -> None:
    base = _hashes(CFG)
    changed = _hashes(CFG.replace("Box('b', size=2", "Box('b', size=99"))
    a = "default:test.Box:a"
    b = "default:test.Box:b"
    assert changed[a] == base[a]  # unrelated node unchanged
    assert changed[b] != base[b]  # changed node differs


def test_dependency_change_propagates_to_dependent() -> None:
    base = _hashes(CFG)
    # change the upstream 'a' input -> its hash changes AND 'b' (depends on a) changes
    changed = _hashes(CFG.replace("Box('a', size=1)", "Box('a', size=7)"))
    a = "default:test.Box:a"
    b = "default:test.Box:b"
    assert changed[a] != base[a]
    assert changed[b] != base[b]


def test_independent_nodes_do_not_affect_each_other() -> None:
    cfg = "Box('a', size=1)\nBox('b', size=2)\n"
    base = _hashes(cfg)
    changed = _hashes(cfg.replace("Box('a', size=1)", "Box('a', size=5)"))
    assert changed["default:test.Box:b"] == base["default:test.Box:b"]
    assert changed["default:test.Box:a"] != base["default:test.Box:a"]
