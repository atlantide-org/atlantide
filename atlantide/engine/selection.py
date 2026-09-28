"""Which nodes a run acts on: ``--target``, ``--replace`` and ``--env`` narrowing.

Pure functions of a compiled config and the prior state; nothing here reads a
backend or a provider.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlantide.core.node_id import stack_of
from atlantide.engine.model import Compiled
from atlantide.graph.order import topological_order
from atlantide.graph.select import TargetError, closure, match_targets
from atlantide.reconcile import ImportRequest
from atlantide.reconcile.state_ir import state_digraph
from atlantide.state import StateGraph


def known_ids(compiled: Compiled, prior: StateGraph) -> set[str]:
    """Every node id a pattern may name: the desired graph plus what state holds.

    Ids only in state are matchable too: a resource being deleted has no node in
    the config.
    """
    return set(compiled.graph.node_ids) | set(prior.nodes)


def target_selection(
    compiled: Compiled, prior: StateGraph, patterns: Sequence[str]
) -> frozenset[str] | None:
    """Node ids the patterns name, closed over their dependencies.

    ``None`` when no patterns are given, as distinct from an empty set, which
    means "act on nothing".
    """
    if not patterns:
        return None
    seeds = match_targets(patterns, known_ids(compiled, prior))
    return closure(compiled.graph, seeds & set(compiled.graph.node_ids), reverse=False) | seeds


def within_envs(
    compiled: Compiled,
    prior: StateGraph,
    selected: frozenset[str] | None,
    patterns: Sequence[str],
) -> frozenset[str] | None:
    """Drop state nodes belonging to a declared-but-unselected environment.

    Under ``--env prod`` the config declares no dev resources, so every dev
    node in state would otherwise diff as a delete. An unselected environment is
    out of scope rather than undeclared, so its nodes leave the selection.

    Stacks declared outside ``config.envs()``, such as a shared ``common``, are
    not in ``envs_declared`` and are unaffected.
    """
    excluded = set(compiled.envs_excluded)
    if not excluded:
        return selected
    in_scope = frozenset(
        node_id for node_id in known_ids(compiled, prior) if stack_of(node_id) not in excluded
    )
    if selected is None:
        return in_scope
    narrowed = selected & in_scope
    if selected and not narrowed:
        # Otherwise the empty selection reports as "--target matched nothing".
        raise TargetError(
            f"--target {', '.join(patterns)} matched only resources in "
            f"environment(s) {', '.join(compiled.envs_excluded)}, which --env excluded"
        )
    return narrowed


def narrowing(
    compiled: Compiled, prior: StateGraph, targets: Sequence[str], replace: Sequence[str]
) -> tuple[frozenset[str] | None, frozenset[str]]:
    """A plan's ``(selected, forced)`` node sets.

    ``selected`` is what ``--target``/``--env`` leave in scope, or ``None`` when
    nothing narrowed it; ``forced`` is what ``--replace`` names.
    """
    selected = target_selection(compiled, prior, targets)
    selected = within_envs(compiled, prior, selected, targets)
    return selected, match_only(compiled, prior, replace)


def match_only(compiled: Compiled, prior: StateGraph, patterns: Sequence[str]) -> frozenset[str]:
    """Exactly the node ids the patterns name, without dependency closure.

    ``--replace`` recreates only the named nodes. Closing over dependencies, as
    ``--target`` does, would force-replace the whole upstream tree (for a subnet,
    its VPC and everything else in it).
    """
    if not patterns:
        return frozenset()
    return match_targets(patterns, known_ids(compiled, prior))


def destroy_selection(prior: StateGraph, patterns: Sequence[str]) -> frozenset[str]:
    """Targets plus everything that still depends on them, from state alone.

    The desired graph is empty during a destroy, so the edges come from what
    each stored node recorded as its dependencies.
    """
    seeds = match_targets(patterns, set(prior.nodes))
    return closure(state_digraph(prior), seeds, reverse=True)


def import_order(compiled: Compiled, requests: Sequence[ImportRequest]) -> list[ImportRequest]:
    """Requests in dependency order; ones naming no known node keep their place.

    Unknown node ids are not rejected here: ``adopt`` reports each per request, so
    one bad id does not abort the rest of the batch.
    """
    rank = {node_id: i for i, node_id in enumerate(topological_order(compiled.graph))}
    return sorted(requests, key=lambda r: rank.get(r.node_id, len(rank)))
