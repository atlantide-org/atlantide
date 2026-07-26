"""Selecting a subset of the graph: ``--target`` and its dependency closure.

Targeting one resource never means only that resource. A subnet cannot be created
before its VPC, and a VPC cannot be destroyed while a subnet still references it —
so a selection is always closed over the graph, and which direction it closes in
depends on what is about to happen to it.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch

from atlantide.core.errors import AtlantideError
from atlantide.core.node_id import short_id
from atlantide.graph.model import DiGraph


class TargetError(AtlantideError):
    """A ``--target`` pattern matched nothing.

    Deliberately an error rather than an empty selection: a typo would otherwise
    read as "did everything you asked for", having done nothing — and the next
    thing the operator does is assume the resource is fine.
    """


def closure(graph: DiGraph, seeds: Iterable[str], *, reverse: bool) -> frozenset[str]:
    """``seeds`` plus everything they transitively require, in one direction.

    ``reverse=False`` (create/update) walks *dependencies*: acting on a node means
    acting on what it is built from. ``reverse=True`` (destroy) walks *dependents*:
    removing a node means removing what still points at it.
    """
    selected: set[str] = set()
    stack = list(seeds)
    while stack:
        node_id = stack.pop()
        if node_id in selected:
            continue
        selected.add(node_id)
        stack.extend(graph.dependents[node_id] if reverse else graph.deps.get(node_id, ()))
    return frozenset(selected)


def match_targets(
    patterns: Iterable[str], known: Iterable[str], *, default_stack: str = "default"
) -> frozenset[str]:
    """Resolve ``--target`` patterns to node ids.

    Three spellings, because a full node id is precise and nobody wants to type
    one: the id itself (``prod:aws.S3Bucket:assets``), the short form against the
    default stack (``aws.S3Bucket:assets``), or an fnmatch glob over either
    (``prod:*`` , ``*:assets``).
    """
    node_ids = list(known)
    selected: set[str] = set()
    for pattern in patterns:
        matched = _match_one(pattern, node_ids, default_stack)
        if not matched:
            raise TargetError(
                f"--target {pattern!r} matched no resource. Use a node id "
                f"(`{node_ids[0]}` if there is one), the short form, or a glob; "
                f"`atlantide state list` shows what exists"
                if node_ids
                else f"--target {pattern!r} matched no resource: nothing is planned"
            )
        selected |= matched
    return frozenset(selected)


def _match_one(pattern: str, node_ids: list[str], default_stack: str) -> set[str]:
    if pattern in node_ids:
        return {pattern}
    scoped = f"{default_stack}:{pattern}"
    if scoped in node_ids:
        return {scoped}
    return {
        node_id
        for node_id in node_ids
        if fnmatch(node_id, pattern) or fnmatch(short_id(node_id), pattern)
    }
