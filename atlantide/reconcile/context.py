"""Shared execution context for apply/refresh: environments, callbacks, phases.

``ApplyEnv`` bundles the long-lived services a run needs; ``Desired`` bundles one
compiled config's per-run artifacts. Both are frozen — a run never mutates its
environment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from returns.pipeline import is_successful

from atlantide.core.actions import Action
from atlantide.core.errors import AtlantideError, ProviderError
from atlantide.core.events import ApplyEvent, EventSink, no_sink
from atlantide.core.provider import Provider
from atlantide.core.registry import ProviderRegistry
from atlantide.core.resource import Resource
from atlantide.graph import build_graph
from atlantide.graph.model import DiGraph
from atlantide.graph.schedule import DEFAULT_PARALLELISM
from atlantide.ir.model import IRGraph, IRNode
from atlantide.secrets import SecretsRegistry
from atlantide.state.backend import LeaseGuard, StateBackend, StateGraph

#: Live per-node computed values during a run: node id -> {attr: value}.
LiveOutputs = dict[str, dict[str, Any]]

OnFailure = Literal["halt", "rollback"]

#: Progress phases reported for each node (see the callbacks below).
PHASE_START = "start"
PHASE_FINISH = "finish"
PHASE_FAIL = "fail"

#: Refresh progress callback: ``(node_id, phase)``.
RefreshProgress = Callable[[str, str], None]

#: Apply progress callback: ``(node_id, action, phase)``. Invoked from
#: concurrent tasks in one asyncio thread.
ProgressCallback = Callable[[str, Action, str], None]

#: Default ceiling on one node's reconcile, in seconds.
#:
#: Sized to clear the slowest legitimate single-node operation: a CloudFront
#: distribution polls for up to 30 minutes to reach ``Deployed``.
#:
#: This cancels the *await*, not the provider call: handlers run in a worker
#: thread via :func:`asyncio.to_thread`, and a thread cannot be killed, so the
#: boto call runs until its own socket timeout fires (see
#: :mod:`atlantide.providers.aws.config`). Together they bound a hang.
DEFAULT_NODE_TIMEOUT = 2400.0


@dataclass(frozen=True, slots=True)
class ApplyEnv:
    """The services and settings shared by every node of a run."""

    types: dict[str, type[Resource]]
    providers: ProviderRegistry
    backend: StateBackend
    secrets: SecretsRegistry
    stack_outputs: dict[str, Any] = field(default_factory=dict)
    parallelism: int = DEFAULT_PARALLELISM
    #: Checked before every state write. The default guard holds no lease and never
    #: refuses — correct for unlocked paths (read-only refresh, tests) and for an
    #: embedding caller that does its own locking.
    lease: LeaseGuard = field(default_factory=LeaseGuard)
    #: Where run events go. The default discards, so nothing pays for the
    #: stream unless a caller wants it. See :mod:`atlantide.core.events`.
    events: EventSink = no_sink
    #: Identifies this run in every event it emits. Supplied by the caller
    #: (the CLI uses the lock owner, which already encodes host + pid + token).
    run_id: str = ""
    #: Ceiling on one node's whole reconcile. Without it a hung provider call
    #: hangs the apply for the life of the process, holding its lease throughout.
    #: Generous by default: some control-plane operations take many minutes, so
    #: this is a backstop against "never", not a service-level target.
    node_timeout: float = DEFAULT_NODE_TIMEOUT


@dataclass(frozen=True, slots=True)
class Desired:
    """One compiled config's per-run artifacts, as the executor consumes them."""

    ir: IRGraph
    graph: DiGraph
    hashes: dict[str, str]
    resources: dict[str, Resource]
    output_decls: dict[str, Any] = field(default_factory=dict)


def provider_for(providers: ProviderRegistry, name: str) -> Provider:
    """The registered provider named ``name``, or a `ProviderError` naming it.

    Tested with `is_successful` rather than truthiness: a `Failure` is an ordinary
    object with no ``__bool__``, so ``if not resolved`` is always false and the
    message below was unreachable — the operator got `unwrap`'s bare
    `UnwrapFailedError`, which carries no indication of which provider was missing.
    """
    resolved = providers.get(name)
    if not is_successful(resolved):
        raise ProviderError(f"no provider registered for {name!r}")
    return resolved.unwrap()


def node_failure(node_id: str, op: str, exc: BaseException) -> AtlantideError:
    """Tag a node's failure with its id so callers can trace which resource broke.

    A :class:`ProviderError` is annotated in place (preserving its type, message,
    and ``__cause__``); any other atlantide error passes through untouched; a raw
    exception is wrapped in a ``ProviderError`` carrying the node id and op."""
    if isinstance(exc, ProviderError):
        if exc.node_id is None:
            exc.node_id = node_id
        return exc
    if isinstance(exc, AtlantideError):
        return exc
    return ProviderError(f"{op} of {node_id!r} failed: {exc}", node_id=node_id, op=op)


def ir_from_state(
    state: StateGraph,
    *,
    with_properties: bool = False,
    ignore_changes: Mapping[str, tuple[str, ...]] | None = None,
) -> IRGraph:
    """Reconstruct an :class:`IRGraph` from persisted state.

    Edges to ids absent from state (e.g. a dependency removed by a partial
    rollback) are dropped — a missing dependency can neither order a delete nor be
    hashed. ``with_properties``/``ignore_changes`` are needed only when the result
    feeds the Merkle hash (properties are irrelevant to delete ordering).
    """
    present = set(state.nodes)
    ignore = ignore_changes or {}
    nodes = tuple(
        IRNode(
            id=n.id,
            type=n.type,
            provider=n.provider,
            provider_version=n.provider_version,
            properties=n.properties if with_properties else {},
            dependencies=tuple(dep for dep in n.dependencies if dep in present),
            ignore_changes=ignore.get(n.id, ()),
        )
        for n in state.nodes.values()
    )
    return IRGraph(nodes=nodes)


def state_digraph(state: StateGraph) -> DiGraph:
    """Rebuild the dependency graph recorded in state (for delete ordering)."""
    # State always came from an acyclic apply, so rebuilding its graph cannot cycle.
    return build_graph(ir_from_state(state)).unwrap()


def progress_sink(callback: ProgressCallback) -> EventSink:
    """Adapt an old-style progress callback onto the event stream.

    The TUI keeps its narrow signature and the executor gains one emission path
    instead of two. Two would drift: a phase added to one and forgotten in the
    other is invisible until a display stops updating.
    """
    phases = {
        "node_start": PHASE_START,
        "node_finish": PHASE_FINISH,
        "node_fail": PHASE_FAIL,
    }

    def emit(event: ApplyEvent) -> None:
        phase = phases.get(event.phase)
        if phase is None or event.node_id is None or event.action is None:
            return  # run- and lease-level events have nothing to draw
        callback(event.node_id, Action(event.action), phase)

    return emit
