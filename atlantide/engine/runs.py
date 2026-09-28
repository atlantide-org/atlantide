"""The locked-run scaffold: every mutating engine run goes through here.

Apply, destroy, ``refresh --write`` and import all run under
:meth:`LockedRuns.run_locked`, so they share the fresh lease guard, the owner and
run-id wiring, the state writer and the post-run checkpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError, PolicyViolationError, ProviderRegistry, Resource
from atlantide.core.errors import LockError
from atlantide.core.events import EventSink
from atlantide.core.logging import get_logger
from atlantide.core.tuning import DEFAULT_PARALLELISM
from atlantide.engine.locking import lock_owner, with_lock
from atlantide.engine.model import Plan
from atlantide.ir.model import IRGraph
from atlantide.reconcile import (
    ApplyEnv,
    ApplyReport,
    ChangeSet,
    Desired,
    OnFailure,
    ProgressCallback,
    apply,
    persist_migration,
    resolve_aliases,
)
from atlantide.reconcile.writer import (
    SerializedBackend,
    StateWriter,
    state_writer,
    writer_installed,
)
from atlantide.secrets import SecretsRegistry
from atlantide.state import LeaseGuard, LockPolicy, StateBackend, StateGraph

#: Fixed logger name: the checkpoint warning is logged as ``atlantide.engine``.
_log = get_logger("engine")

#: A changeset bound to the executor, awaiting the prior state read under the lease.
type Runner = Callable[[StateGraph], Awaitable[ApplyReport]]


class RunHost(Protocol):
    """The engine settings a locked run reads, at the moment it runs.

    Read through the host rather than copied, because callers reassign them
    between runs (the CLI sets ``events`` after building the engine).
    """

    providers: ProviderRegistry
    backend: StateBackend
    types: dict[str, type[Resource]]
    parallelism: int | None
    lock_policy: LockPolicy
    node_timeout: float
    secrets: SecretsRegistry
    events: EventSink
    run_id: str


class LockedRuns:
    """Runs work under the state lock on behalf of one engine."""

    def __init__(self, host: RunHost) -> None:
        self.host = host
        # Lease of the run currently under `run_locked`, passed to the executor by
        # `env` as a write guard. The default holds no lease and never refuses, for
        # the unlocked paths (a read-only refresh, an embedding caller).
        self.lease = LeaseGuard(grace=host.lock_policy.renew_grace)

    def stack_outputs(self) -> dict[str, Any]:
        """Committed cross-stack outputs, with any sealed sensitive value unsealed."""
        secrets = self.host.secrets
        return {k: secrets.unseal(v) for k, v in self.host.backend.outputs().items()}

    def env(self) -> ApplyEnv:
        """The run environment; ``stack_outputs`` snapshots committed outputs now."""
        host = self.host
        extra: dict[str, Any] = {"parallelism": host.parallelism} if host.parallelism else {}
        return ApplyEnv(
            types=host.types,
            providers=host.providers,
            backend=host.backend,
            secrets=host.secrets,
            stack_outputs=self.stack_outputs(),
            lease=self.lease,
            node_timeout=host.node_timeout,
            events=host.events,
            run_id=host.run_id,
            **extra,
        )

    def runner(
        self,
        changeset: ChangeSet,
        desired: Desired,
        on_failure: OnFailure = "halt",
        progress: ProgressCallback | None = None,
    ) -> Runner:
        """Bind a changeset to the shared executor; ``run_locked`` supplies prior state."""

        def run(prior: StateGraph) -> Awaitable[ApplyReport]:
            return apply(
                changeset=changeset,
                desired=desired,
                prior=prior,
                env=self.env(),
                on_failure=on_failure,
                progress=progress,
            )

        return run

    def runner_for_plan(
        self,
        plan_obj: Plan,
        on_failure: OnFailure = "halt",
        progress: ProgressCallback | None = None,
    ) -> Result[Runner, AtlantideError]:
        """A runner for ``plan_obj``, or the policy denial that forbids running it."""
        if plan_obj.blocked:
            joined = "; ".join(f"{v.policy}: {v.message}" for v in plan_obj.blocked)
            return Failure(
                PolicyViolationError(f"policy denied apply: {joined}", list(plan_obj.blocked))
            )
        return Success(
            self.runner(plan_obj.changeset, plan_obj.compiled.desired(), on_failure, progress)
        )

    async def run_locked[T](
        self,
        run: Callable[[StateGraph], Awaitable[T]],
        scope: frozenset[str],
        *,
        prepare: Callable[[StateGraph], StateGraph] | None = None,
    ) -> Result[T, AtlantideError]:
        """Run under the state lock, feeding ``run`` the state loaded post-acquire.

        Callers size ``scope`` from a snapshot read before the lock, which may be
        stale once the lease is held. ``run`` therefore receives only the state
        re-read here; acting on the snapshot could duplicate a resource or rewrite
        a destroyed row.

        ``prepare`` (apply only) may first rewrite persisted state, such as the
        alias rekey, and returns the state ``run`` receives.

        Refuses with a ``LockError`` while another run is using the same backend
        instance, since the instance's bound lease and writer are per run.
        """
        host = self.host
        busy = _backend_in_use(host.backend)
        if busy is not None:
            return Failure(busy)
        # Fresh guard per locked run: `env` reads it when building the executor,
        # after the lease is taken.
        self.lease = LeaseGuard(grace=host.lock_policy.renew_grace)
        # The lock owner encodes host, pid and a per-run token, so it also serves
        # as the run id in audit records.
        host.run_id = lock_owner()
        # The executor's state writes go through this writer (off the event loop
        # for a network backend); the lock scaffold's calls are queued on it too,
        # so the two never reach the backend at once.
        with state_writer(
            host.backend, parallelism=host.parallelism or DEFAULT_PARALLELISM
        ) as writer:
            lock_backend = (
                SerializedBackend(host.backend, writer) if writer.offloaded else host.backend
            )
            return await with_lock(
                lock_backend,
                scope,
                lambda: self._run_then_checkpoint(run, prepare, writer),
                policy=host.lock_policy,
                guard=self.lease,
                owner=host.run_id,
                events=host.events,
                run_id=host.run_id,
            )

    async def _run_then_checkpoint[T](
        self,
        run: Callable[[StateGraph], Awaitable[T]],
        prepare: Callable[[StateGraph], StateGraph] | None,
        writer: StateWriter,
    ) -> T:
        """The body of a locked run: load state, ``run`` it, then :meth:`checkpoint`."""
        prior = self.host.backend.load()
        try:
            # The only place a missing install key may be created: this run
            # writes digests and sealed values, so a key it creates is the one
            # later runs need. Creation stays lazy, so a run that digests and
            # seals nothing leaves no keyfile.
            with self.host.secrets.creating_key():
                result = await run(prepare(prior) if prepare is not None else prior)
        except asyncio.CancelledError:
            # Interrupted or lease lost: skip the checkpoint, since this run may
            # no longer hold the lock.
            raise
        except BaseException:
            with contextlib.suppress(asyncio.CancelledError):
                await self.checkpoint(writer)
            raise
        await self.checkpoint(writer)
        return result

    async def checkpoint(self, writer: StateWriter) -> None:
        """Let the backend fold this run's writes, best-effort, while the lease is held.

        Runs after the run body, on success or failure, so every write has landed
        (the executor awaits each one, and a cancelled one settles first), and
        before the release, so the backend compacts against heads no other run
        can move. Goes through the writer like any write: off the loop for a
        network backend, and ordered with the heartbeat by the same gate.

        Never fails the run or masks its error: the writes are already durable, so
        a failed checkpoint is logged as a warning. Skipped when the backend has no
        callable ``checkpoint`` or the lease was lost.
        """
        checkpoint = getattr(self.host.backend, "checkpoint", None)
        if checkpoint is None or not callable(checkpoint) or self.lease.lost is not None:
            return
        try:
            await writer.run(checkpoint)
        except Exception as exc:
            _log.warning(
                "state checkpoint failed; the run's writes are durable and the "
                "housekeeping will be retried after a later run: %s: %s",
                type(exc).__name__,
                exc,
            )

    def alias_migration(self, ir: IRGraph) -> Callable[[StateGraph], StateGraph]:
        """A ``run_locked`` prepare hook that persists any alias rekey.

        The executor and later runs then see the renamed nodes' new ids.
        """

        def prepare(prior: StateGraph) -> StateGraph:
            migrated, remap = resolve_aliases(prior, ir)
            if not remap:
                return prior
            persist_migration(self.host.backend, prior, migrated, remap)
            return self.host.backend.load()

        return prepare


def _backend_in_use(backend: StateBackend) -> LockError | None:
    """Why a locked run cannot start on ``backend`` now, or ``None`` if it can.

    A backend instance holds one bound lease and one installed writer, so two
    runs sharing it at once would fence each other's writes on the wrong lease
    and queue on each other's writer, even over disjoint lock scopes.
    """
    # `_lease` is the base class's record of the bound lease (see `bind_lease`).
    if writer_installed(backend) or getattr(backend, "_lease", None) is not None:
        return LockError(
            f"another run is already running on this state backend instance "
            f"({backend!r}); concurrent runs need a backend instance each"
        )
    return None
