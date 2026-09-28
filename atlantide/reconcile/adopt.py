"""Adopt an existing cloud resource into state without creating it.

The engine manages only what state records, and an apply writes a row only by
creating the resource. Without import, the first apply creates a second copy of
existing infrastructure even when config describes it exactly.

The user declares the resource in config and names the node. Adoption reads the
live resource through the provider, checks it against config, and writes the
:class:`~atlantide.state.model.StateNode` an apply would write, so the next plan
reports NOOP rather than CREATE.

Adoption is anchored on config rather than a type-and-id pair because
:meth:`~atlantide.core.provider.Provider.read` takes a *resource*, not an id. The
row then carries the same Merkle ``input_hash`` an apply computes, so the next
plan skips it.

Distinct from :func:`~atlantide.providers.aws.handlers.faults.create_or_adopt`,
a fallback *inside* a create for a resource this node already made. Nothing here
calls a mutating provider method.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from atlantide.core.context import Context
from atlantide.core.fields import sensitive_fields
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.ir.model import IRGraph, IRNode
from atlantide.reconcile.applied import ref_digests
from atlantide.reconcile.env import ApplyEnv, LiveOutputs, provider_for
from atlantide.reconcile.refresh import Drift, NodeDrift, classify_drift, resolved_properties
from atlantide.reconcile.resolve import (
    live_outputs,
    reconstruct,
    seal_outputs,
    secret_digests,
)
from atlantide.state import (
    NO_INPUT_HASH,
    NodeStatus,
    StateGraph,
    StateNode,
)


class ImportStatus(StrEnum):
    """Result of one import request.

    An enum so a renderer can cover every case, as with
    :class:`~atlantide.reconcile.refresh.Drift`.
    """

    #: The row was written; the next plan will report this node unchanged.
    IMPORTED = "imported"
    #: A dry run: everything checked out, nothing was written.
    WOULD_IMPORT = "would_import"
    #: The live resource does not match what config declares. Nothing written.
    DRIFTED = "drifted"
    #: The provider found no such resource. Nothing written.
    NOT_FOUND = "not_found"
    #: Already in state. Nothing written unless ``force``.
    ALREADY_TRACKED = "already_tracked"
    #: Cannot be attempted (unknown node, missing dependency, or missing id), or
    #: the read failed; ``detail`` says which.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """One node to adopt, and the id to find it by if its type needs one."""

    node_id: str
    external_id: str | None = None
    #: Overrides the provider's declared identity field, for a type whose read
    #: keys on another field.
    id_field: str | None = None


@dataclass(frozen=True, slots=True)
class AdoptOptions:
    """How a batch treats what it finds; the same for every request in it."""

    #: Record each adopted row. ``False`` is a dry run: everything is checked and
    #: nothing is written.
    write: bool = True
    #: Adopt a resource whose live state differs from config instead of refusing
    #: it. The row is then poisoned so the next plan shows the drift.
    allow_drift: bool = False
    #: Re-adopt a node state already tracks, overwriting its row.
    force: bool = False


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What happened to one request."""

    node_id: str
    type: str
    status: ImportStatus
    identity_field: str | None = None
    external_id: str | None = None
    drift: NodeDrift | None = None
    #: Names of the recorded outputs; values are omitted because they may be sealed.
    recorded: tuple[str, ...] = ()
    detail: str = ""

    @property
    def wrote_state(self) -> bool:
        return self.status is ImportStatus.IMPORTED

    @property
    def unresolved(self) -> bool:
        """Whether this request ended without adopting a resource it could have.

        A domain fact, not an exit code: the CLI interprets it, as it does
        :attr:`~atlantide.reconcile.refresh.DriftReport.has_drift`.
        """
        return self.status in (
            ImportStatus.DRIFTED,
            ImportStatus.NOT_FOUND,
            ImportStatus.BLOCKED,
        )

    @property
    def unobserved(self) -> tuple[str, ...]:
        """Fields the provider's read did not report.

        The import's match verdict does not cover these fields. Derived from
        ``drift`` rather than stored, so it always agrees with the drift verdict.
        """
        return self.drift.unobserved if self.drift else ()


async def adopt(
    *,
    requests: Sequence[ImportRequest],
    ir: IRGraph,
    hashes: Mapping[str, str],
    prior: StateGraph,
    env: ApplyEnv,
    options: AdoptOptions,
) -> list[ImportOutcome]:
    """Adopt each request in turn and return one outcome per request.

    Requests run sequentially in the given order, which the caller sorts
    topologically: a node's ``$ref`` inputs resolve against its dependencies'
    outputs, so a VPC must be adopted before a subnet that references it can be read.

    A failed request writes nothing and does not stop later requests, so one run
    reports every problem in the batch and a partial adoption is resumable.
    """
    session = _Session(
        env=env,
        hashes=hashes,
        nodes={node.id: node for node in ir.nodes},
        # Seeded from committed state and extended per adopted node, so a ref to a
        # node adopted earlier in this batch resolves.
        outputs=live_outputs(prior, env.secrets),
        tracked=set(prior.nodes),
        options=options,
    )
    return [await session.adopt(request) for request in requests]


@dataclass(slots=True)
class _Session:
    """Shared context for one batch.

    ``outputs`` and ``tracked`` carry state from one node's adoption to the next.
    """

    env: ApplyEnv
    hashes: Mapping[str, str]
    nodes: Mapping[str, IRNode]
    outputs: LiveOutputs
    tracked: set[str]
    options: AdoptOptions
    ctx: Context = field(default_factory=Context)

    async def adopt(self, request: ImportRequest) -> ImportOutcome:
        """Check, read, compare and record one node."""
        node = self.nodes.get(request.node_id)
        if node is None:
            return ImportOutcome(
                request.node_id, "", ImportStatus.BLOCKED, detail="not in this config"
            )

        step = _Adoption(session=self, request=request, node=node)
        if (refusal := step.refusal()) is not None:
            return refusal
        return await step.run()


@dataclass(slots=True)
class _Adoption:
    """One node's adoption, holding the context shared by every step and outcome."""

    session: _Session
    request: ImportRequest
    node: IRNode
    identity_field: str | None = None

    # -- the checks that can refuse before anything is read ----------------

    def refusal(self) -> ImportOutcome | None:
        """The first reason this node cannot be adopted, or ``None`` to proceed.

        Also resolves ``identity_field``, which two of the checks depend on.
        """
        session = self.session
        if self.node.id in session.tracked and not session.options.force:
            return self.outcome(ImportStatus.ALREADY_TRACKED, detail="already in state")
        if missing := [dep for dep in sorted(self.node.dependencies) if dep not in session.tracked]:
            # A `$ref` to a node with no recorded outputs does not resolve, so the
            # read would match on partially resolved inputs and find nothing or an
            # unrelated resource.
            return self.outcome(
                ImportStatus.BLOCKED,
                detail=f"depends on nodes not in state yet: {', '.join(missing)}",
            )
        if self.resource_type is None:
            return self.outcome(ImportStatus.BLOCKED, detail=f"unknown type {self.node.type!r}")

        self.identity_field = self.request.id_field or self.provider.identity_field(
            self.resource_type
        )
        if self.identity_field and not self.request.external_id:
            return self.outcome(
                ImportStatus.BLOCKED,
                detail=(
                    f"{self.node.type} is located by its {self.identity_field}, which config "
                    f"cannot know — pass the id as the second argument"
                ),
            )
        if not self.identity_field and self.request.external_id:
            return self.outcome(
                ImportStatus.BLOCKED, detail=f"{self.node.type} is found by name; it takes no id"
            )
        return None

    # -- the read, the comparison, and the write ---------------------------

    async def run(self) -> ImportOutcome:
        """Read the live resource, compare it to config, and record it."""
        # The id goes on its computed field, as in a row an apply writes, so
        # `read` receives its usual input shape.
        seed = {self.identity_field: self.request.external_id} if self.identity_field else {}
        try:
            res = reconstruct(self.row(seed), self.session.env, self.session.outputs)
            live = await self.provider.read(self.session.ctx, res)
        except Exception as exc:
            # This request only: nothing was written, and the batch goes on.
            return self.outcome(
                ImportStatus.BLOCKED, detail=f"read failed: {type(exc).__name__}: {exc}"
            )
        if live is None:
            return self.outcome(
                ImportStatus.NOT_FOUND, detail="the provider found no such resource"
            )

        recorded = self.split_outputs(live)
        drift = self.compare(res, live, recorded)
        if drift.kind is Drift.DRIFTED and not self.session.options.allow_drift:
            return self.outcome(
                ImportStatus.DRIFTED,
                drift=drift,
                detail="the live resource differs from what config declares",
            )

        if self.session.options.write:
            self.persist(res, recorded, poisoned=drift.kind is Drift.DRIFTED)
        else:
            # A dry run writes no state, but later requests must see what a real
            # run would: the dependency and ALREADY_TRACKED checks read
            # ``tracked``, and dependents' ``$ref``s resolve through ``outputs``.
            self.session.tracked.add(self.node.id)
            self.session.outputs[self.node.id] = recorded
        return self.outcome(
            ImportStatus.IMPORTED if self.session.options.write else ImportStatus.WOULD_IMPORT,
            drift=drift,
            recorded=tuple(sorted(recorded)),
        )

    def split_outputs(self, live: dict[str, Any]) -> dict[str, Any]:
        """The part of the read that belongs in ``outputs`` rather than ``properties``.

        A read reports inputs and computed values in one mapping: a differing input
        is drift, while a computed value is this resource's identity. Recording a
        reported input as an output would shadow that input on every later refresh;
        :func:`~atlantide.reconcile.refresh._folded` splits a read the same way.
        """
        recorded = {k: v for k, v in live.items() if k not in self.node.properties}
        if self.identity_field and self.request.external_id:
            recorded.setdefault(self.identity_field, self.request.external_id)
        return recorded

    def compare(self, res: Resource, live: dict[str, Any], recorded: dict[str, Any]) -> NodeDrift:
        """Compare the live resource against what config declares.

        Uses a probe row carrying the *unsealed* outputs: ``classify_drift`` unseals
        whatever it receives, and these values are not sealed yet.
        """
        assert self.resource_type is not None  # refusal() proved it
        probe = self.row(recorded)
        return classify_drift(
            probe,
            resolved_properties(probe, res),
            live,
            frozenset(sensitive_fields(self.resource_type)),
            self.session.env.secrets,
        )

    def persist(self, res: Resource, recorded: dict[str, Any], *, poisoned: bool) -> None:
        """Write the row, and let the nodes after this one resolve refs to it."""
        assert self.resource_type is not None  # refusal() proved it
        env = self.session.env
        row = self.row(
            seal_outputs(recorded, self.resource_type, env.secrets),
            digests=secret_digests(res, self.node.id, env.secrets),
            # What the refs resolve to now: the read matched config (or the row is
            # poisoned below), so the live resource holds these values.
            refs=ref_digests(
                self.node.type,
                self.node.properties,
                self.session.outputs,
                types=env.types,
                secrets=env.secrets,
            ),
            # Drift adopted under `allow_drift` must reach the next plan, but
            # config and state hash identically. Clearing the hash surfaces it, as
            # `refresh --write` does.
            poison=poisoned,
        )
        env.lease.check()  # a lost lease must not write state
        env.backend.put(row)
        self.session.tracked.add(self.node.id)
        self.session.outputs[self.node.id] = recorded

    # -- shared pieces -----------------------------------------------------

    @property
    def provider(self) -> Provider:
        return provider_for(self.session.env.providers, self.node.provider)

    @property
    def resource_type(self) -> type[Resource] | None:
        return self.session.env.types.get(self.node.type)

    def outcome(self, status: ImportStatus, **detail: Any) -> ImportOutcome:
        """An outcome with this node's identity already filled in."""
        return ImportOutcome(
            self.node.id,
            self.node.type,
            status,
            identity_field=self.identity_field,
            external_id=self.request.external_id,
            **detail,
        )

    def row(
        self,
        outputs: dict[str, Any],
        *,
        digests: dict[str, str] | None = None,
        refs: dict[str, str] | None = None,
        poison: bool = False,
    ) -> StateNode:
        """The state row for this node, matching the row the executor writes.

        ``input_hash`` is the Merkle hash from the compile, never recomputed here.
        The diff compares against it, so an unchanged config skips this node
        without a provider call.

        ``properties`` keeps the IR's symbolic form, including ``$ref`` and
        ``$secret_ref`` markers. Resolved values would drop the dependency from
        state, and the next config change would compare a marker against a literal,
        planning a spurious REPLACE on any ``immutable()`` field.
        """
        node = self.node
        return StateNode(
            id=node.id,
            type=node.type,
            provider=node.provider,
            provider_version=node.provider_version,
            input_hash=NO_INPUT_HASH if poison else self.session.hashes[node.id],
            outputs=outputs,
            properties=node.properties,
            dependencies=node.dependencies,
            depends_on=node.depends_on,
            prevent_destroy=node.prevent_destroy,
            secret_digests=digests or {},
            ref_digests=refs or {},
            # Not `creating`: the next plan re-creates a write-ahead row instead of
            # skipping it.
            status=NodeStatus.CREATED,
        )


def identity_fields(
    *, ir: IRGraph, types: Mapping[str, type[Resource]], providers: Any, node_ids: Sequence[str]
) -> dict[str, str | None]:
    """The id field each node's type is located by, or ``None`` if found by name.

    Computed from config and resource types alone, with no provider call or
    resource construction.
    """
    by_id = {node.id: node for node in ir.nodes}

    def field_of(node_id: str) -> str | None:
        node = by_id.get(node_id)
        cls = types.get(node.type) if node is not None else None
        if node is None or cls is None:
            return None
        return provider_for(providers, node.provider).identity_field(cls)

    return {node_id: field_of(node_id) for node_id in node_ids}
