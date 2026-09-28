"""What a run executes against: ``ApplyEnv`` (the services) and ``Desired`` (the config).

Both are frozen: a run never mutates its environment. Also defines the two helpers
every per-node step needs: :func:`provider_for` and :func:`node_failure`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from returns.pipeline import is_successful

from atlantide.core.errors import AtlantideError, ProviderError
from atlantide.core.events import EventSink, no_sink
from atlantide.core.provider import Provider
from atlantide.core.registry import ProviderRegistry
from atlantide.core.resource import Resource
from atlantide.graph import build_graph
from atlantide.graph.model import DiGraph
from atlantide.graph.schedule import DEFAULT_PARALLELISM
from atlantide.ir.model import IRGraph
from atlantide.secrets import SecretsRegistry
from atlantide.state import LeaseGuard, StateBackend

__all__ = [
    "DEFAULT_NODE_TIMEOUT",
    "ApplyEnv",
    "Desired",
    "LiveOutputs",
    "OnFailure",
    "node_failure",
    "provider_for",
]

#: Live per-node computed values during a run: node id -> {attr: value}.
type LiveOutputs = dict[str, dict[str, Any]]

OnFailure = Literal["halt", "rollback"]

#: Default ceiling on one node's reconcile, in seconds.
#:
#: Exceeds the slowest single-node operation: a CloudFront distribution polls for
#: up to 30 minutes to reach ``Deployed``.
#:
#: This cancels the *await*, not the provider call: handlers run in a worker
#: thread via :func:`asyncio.to_thread`, which cannot be killed, so the boto call
#: runs until its own socket timeout fires (see
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
    #: refuses, for unlocked paths (read-only refresh, tests) and for an embedding
    #: caller that does its own locking.
    lease: LeaseGuard = field(default_factory=LeaseGuard)
    #: Where run events go. The default discards them. See
    #: :mod:`atlantide.core.events`.
    events: EventSink = no_sink
    #: Identifies this run in every event it emits. Supplied by the caller
    #: (the CLI uses the lock owner, which already encodes host + pid + token).
    run_id: str = ""
    #: Ceiling on one node's whole reconcile, in seconds. Bounds a hung provider
    #: call, which otherwise holds the apply and its lease indefinitely. A backstop
    #: against hangs, not a service-level target.
    node_timeout: float = DEFAULT_NODE_TIMEOUT


@dataclass(frozen=True, slots=True)
class Desired:
    """One compiled config's per-run artifacts, as the executor consumes them."""

    ir: IRGraph
    graph: DiGraph
    hashes: dict[str, str]
    resources: dict[str, Resource]
    output_decls: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> Desired:
        """A config declaring nothing: what a destroy runs against."""
        ir = IRGraph(nodes=())
        return cls(ir=ir, graph=build_graph(ir).unwrap(), hashes={}, resources={})  # acyclic


def provider_for(providers: ProviderRegistry, name: str) -> Provider:
    """The registered provider named ``name``, or a ``ProviderError`` naming it.

    Tested with ``is_successful``: a ``Failure`` has no ``__bool__``, so a
    truthiness test would never fail and ``unwrap`` would raise a bare error that
    does not name the missing provider.
    """
    resolved = providers.get(name)
    if not is_successful(resolved):
        raise ProviderError(f"no provider registered for {name!r}")
    return resolved.unwrap()


def node_failure(node_id: str, op: str, exc: BaseException) -> AtlantideError:
    """Tag a node's failure with its id so callers can identify the failing resource.

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
