"""Adopt an existing cloud resource into state, without creating anything.

The engine can only manage what state records, and until now the only way to get
a row was to create the resource. That makes an account full of infrastructure
unreachable: the config describes it exactly, and the first apply tries to build
a second copy of everything.

Import closes that. The user declares the resource as normal and names the node;
this reads the live resource through the provider, checks it against what config
declares, and writes the :class:`~atlantide.state.backend.StateNode` an apply
would have written — so the next plan is a NOOP rather than a CREATE.

Anchored on config rather than on a bare type-and-id pair because
:meth:`~atlantide.core.provider.Provider.read` takes a *resource*, not an id:
there is nothing to read with until config has said what the resource is. It also
means the row carries the same Merkle ``input_hash`` an apply would have
computed, which is the whole reason the following plan can skip it.

Distinct from :func:`~atlantide.providers.aws.handlers.base.create_or_adopt`,
which is a fallback *inside* a create for a resource this node already made.
Nothing here calls a mutating provider method at all.
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
from atlantide.reconcile.context import ApplyEnv, LiveOutputs, provider_for
from atlantide.reconcile.refresh import Drift, NodeDrift, classify_drift, resolved_properties
from atlantide.reconcile.resolve import (
    live_outputs,
    reconstruct,
    seal_outputs,
    secret_digests,
)
from atlantide.state.backend import (
    NO_INPUT_HASH,
    STATUS_CREATED,
    StateGraph,
    StateNode,
)


class ImportStatus(StrEnum):
    """What became of one request. An enum rather than loose strings so a renderer
    can cover the set exhaustively, as :class:`~atlantide.reconcile.refresh.Drift`
    already lets the drift report do."""

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
    #: Cannot be attempted — unknown node, missing dependency, or a missing id.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """One node to adopt, and the id to find it by if its type needs one."""

    node_id: str
    external_id: str | None = None
    #: Overrides the provider's declared identity field, for the rare type whose
    #: read keys on something else.
    id_field: str | None = None


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What happened to one request."""

    node_id: str
    type: str
    status: ImportStatus
    identity_field: str | None = None
    external_id: str | None = None
    drift: NodeDrift | None = None
    #: Names of the outputs recorded — names only, since a value may be sealed.
    recorded: tuple[str, ...] = ()
    detail: str = ""

    @property
    def wrote_state(self) -> bool:
        return self.status is ImportStatus.IMPORTED

    @property
    def unresolved(self) -> bool:
        """Whether this request ended without adopting anything it could have.

        A domain fact, not an exit code: the CLI decides what to do with it, as it
        does with :attr:`~atlantide.reconcile.refresh.DriftReport.has_drift`.
        """
        return self.status in (
            ImportStatus.DRIFTED,
            ImportStatus.NOT_FOUND,
            ImportStatus.BLOCKED,
        )

    @property
    def unobserved(self) -> tuple[str, ...]:
        """Fields the provider's read did not report, so this import's "matches
        config" verdict says nothing about them. Derived, never stored: it is the
        drift verdict's own scope and cannot be allowed to disagree with it."""
        return self.drift.unobserved if self.drift else ()


async def adopt(
    *,
    requests: Sequence[ImportRequest],
    ir: IRGraph,
    hashes: Mapping[str, str],
    prior: StateGraph,
    env: ApplyEnv,
    write: bool = True,
    allow_drift: bool = False,
    force: bool = False,
) -> list[ImportOutcome]:
    """Adopt each request in turn, returning one outcome per request.

    Sequential, and in the order given — which the caller sorts topologically.
    A node's ``$ref`` inputs resolve against the outputs of the nodes it depends
    on, so a VPC has to be adopted before the subnet that references it can even
    be read. Concurrency would break that, and there is nothing to gain from it:
    adoption is a one-off, and the reads are few.

    Nothing is written for a request that fails, and a failure does not stop the
    ones after it: a partial adoption is resumable, and stopping at the first
    problem in a twenty-node batch just means finding the problems one per run.
    """
    session = _Session(
        env=env,
        hashes=hashes,
        nodes={node.id: node for node in ir.nodes},
        # Seeded from committed state and extended as this batch proceeds, so a
        # reference to a node adopted moments ago resolves like any other.
        outputs=live_outputs(prior, env.secrets),
        tracked=set(prior.nodes),
        write=write,
        allow_drift=allow_drift,
        force=force,
    )
    return [await session.adopt(request) for request in requests]


@dataclass(slots=True)
class _Session:
    """One batch's shared context, and the per-node steps that run against it.

    A class rather than a chain of parameters because every step needs the same
    six things and the last two — ``outputs`` and ``tracked`` — are what one
    node's adoption hands to the next.
    """

    env: ApplyEnv
    hashes: Mapping[str, str]
    nodes: Mapping[str, IRNode]
    outputs: LiveOutputs
    tracked: set[str]
    write: bool
    allow_drift: bool
    force: bool
    ctx: Context = field(default_factory=Context)

    async def adopt(self, request: ImportRequest) -> ImportOutcome:
        """One node: check it can be adopted, read it, compare it, record it."""
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
    """One node's adoption. Holds what every step and every outcome shares, so
    neither the checks nor the result construction has to pass it around."""

    session: _Session
    request: ImportRequest
    node: IRNode
    identity_field: str | None = None

    # -- the checks that can refuse before anything is read ----------------

    def refusal(self) -> ImportOutcome | None:
        """The first reason this node cannot be adopted, or ``None`` to proceed.

        Resolves ``identity_field`` on the way through, since two of the checks
        are about it.
        """
        session = self.session
        if self.node.id in session.tracked and not session.force:
            return self.outcome(ImportStatus.ALREADY_TRACKED, detail="already in state")
        if missing := [dep for dep in sorted(self.node.dependencies) if dep not in session.tracked]:
            # A `$ref` to a node with no recorded outputs resolves to nothing
            # usable, and the read would then look for a resource whose inputs are
            # half unresolved — matching nothing, or something unrelated.
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
        # Restoring the id onto its computed field is exactly what state does
        # after an apply, so `read` needs no new entry point: it is handed the
        # same shape it always is.
        seed = {self.identity_field: self.request.external_id} if self.identity_field else {}
        res = reconstruct(self.row(seed), self.session.env, self.session.outputs)

        live = await self.provider.read(self.session.ctx, res)
        if live is None:
            return self.outcome(
                ImportStatus.NOT_FOUND, detail="the provider found no such resource"
            )

        recorded = self.split_outputs(live)
        drift = self.compare(res, live, recorded)
        if drift.kind is Drift.DRIFTED and not self.session.allow_drift:
            return self.outcome(
                ImportStatus.DRIFTED,
                drift=drift,
                detail="the live resource differs from what config declares",
            )

        if self.session.write:
            self.persist(res, recorded, poisoned=drift.kind is Drift.DRIFTED)
        else:
            # A dry run writes no state, but later requests in this batch must
            # see the same world the real run would: the dependency and
            # ALREADY_TRACKED checks read ``tracked``, and a dependent's
            # ``$ref``s resolve through ``outputs``. Without this, a request
            # depending on an earlier one reports BLOCKED here and IMPORTED
            # under ``--write`` — a dry run that answers differently.
            self.session.tracked.add(self.node.id)
            self.session.outputs[self.node.id] = recorded
        return self.outcome(
            ImportStatus.IMPORTED if self.session.write else ImportStatus.WOULD_IMPORT,
            drift=drift,
            recorded=tuple(sorted(recorded)),
        )

    def split_outputs(self, live: dict[str, Any]) -> dict[str, Any]:
        """The half of the read that belongs in ``outputs`` rather than ``properties``.

        A read reports inputs and computed values in one mapping, and the two are
        judged differently: an input that differs is drift, a computed value is
        simply this resource's identity. Recording a reported input as an output
        would also shadow the input it mirrors on every later refresh — the
        mistake ``_sync_state`` documents from the other direction.
        """
        recorded = {k: v for k, v in live.items() if k not in self.node.properties}
        if self.identity_field and self.request.external_id:
            recorded.setdefault(self.identity_field, self.request.external_id)
        return recorded

    def compare(self, res: Resource, live: dict[str, Any], recorded: dict[str, Any]) -> NodeDrift:
        """Does the live resource match what config declares?

        Compared through a probe row carrying the *unsealed* outputs, because
        ``classify_drift`` unseals whatever it is given and the values here have
        not been sealed yet.
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
            # Drift adopted under `allow_drift` has to be visible to the next
            # plan, and a symbolic diff cannot see it: config and state hash
            # identically. Clearing the hash is the only channel there is — the
            # same one `refresh --write` uses.
            poison=poisoned,
        )
        env.lease.check()  # as every other state write does; a lost lease must not write
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
        poison: bool = False,
    ) -> StateNode:
        """The state row for this node, field for field as the executor writes it.

        Two of these matter more than the rest:

        ``input_hash`` is the Merkle hash the compile already produced, never one
        recomputed here. It is the exact value the diff compares against, so an
        unchanged config skips this node without a provider call — which is the
        definition of a successful import.

        ``properties`` keeps the IR's symbolic form, ``$ref`` and ``$secret_ref``
        markers included. Substituting the values they resolved to would erase the
        dependency from state, and the next config change would diff a marker
        against a literal — a spurious REPLACE on any ``immutable()`` field.
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
            prevent_destroy=node.prevent_destroy,
            secret_digests=digests or {},
            # Never `creating`: nothing is being created, and a write-ahead row is
            # re-created by the next plan rather than skipped.
            status=STATUS_CREATED,
        )


def identity_fields(
    *, ir: IRGraph, types: Mapping[str, type[Resource]], providers: Any, node_ids: Sequence[str]
) -> dict[str, str | None]:
    """The id field each node's type is located by, or ``None`` if found by name.

    Answers "what would importing this need from me" from the config alone — no
    provider call, and no resource construction either, since the answer is a
    property of the type.
    """
    by_id = {node.id: node for node in ir.nodes}

    def field_of(node_id: str) -> str | None:
        node = by_id.get(node_id)
        cls = types.get(node.type) if node is not None else None
        if node is None or cls is None:
            return None
        return provider_for(providers, node.provider).identity_field(cls)

    return {node_id: field_of(node_id) for node_id in node_ids}
