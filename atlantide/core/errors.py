"""Error taxonomy shared across the engine.

Every atlantide error derives from :class:`AtlantideError` so callers can catch
the whole family with one clause.
"""

from __future__ import annotations


class AtlantideError(Exception):
    """Base class for all atlantide errors."""


class LanguageError(AtlantideError):
    """Atlas-lang source uses a construct outside the allowed subset."""

    def __init__(self, message: str, *, line: int | None = None, col: int | None = None) -> None:
        self.line = line
        self.col = col
        location = f" (line {line}, col {col})" if line is not None else ""
        super().__init__(f"{message}{location}")


class FuelExhaustedError(AtlantideError):
    """Atlas-lang evaluation exceeded its step budget."""


class IRError(AtlantideError):
    """IR construction or canonicalization failed (e.g. non-encodable value)."""


class ArtifactError(AtlantideError):
    """A ``.atlas`` artifact is malformed, corrupted, or fails its hash check."""


class CycleError(AtlantideError):
    """The resource graph contains one or more dependency cycles."""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        rendered = "; ".join(" -> ".join(cycle) for cycle in cycles)
        super().__init__(f"dependency cycle(s) detected: {rendered}")


class StackOutputCycleError(AtlantideError):
    """An in-config cross-stack output reference forms a cycle.

    Raised before lowering (where an infinite substitution recursion would
    otherwise precede the graph's Tarjan cycle check); the chain names the output
    keys involved, e.g. ``common:vpc_id -> dev:x -> common:vpc_id``.
    """

    def __init__(self, chain: list[str]) -> None:
        self.chain = chain
        super().__init__(f"cross-stack output cycle: {' -> '.join(chain)}")


class RegistryError(AtlantideError):
    """Registry lookup/registration failed (unknown name, duplicate, bad version)."""


class ComponentError(AtlantideError):
    """Fetching, vendoring, or verifying a published component failed.

    Covers a bad git source, a missing ``subdir``, and a vendored tree whose
    content hash no longer matches the lock (tamper/drift).
    """


class ProviderError(AtlantideError):
    """A provider CRUD operation failed.

    Optional structured context makes a failure traceable to its origin:
    ``node_id`` (which resource), ``op`` (which CRUD phase), and
    ``resource_type`` (which kind), each defaulting to ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str | None = None,
        op: str | None = None,
        resource_type: str | None = None,
    ) -> None:
        self.node_id = node_id
        self.op = op
        self.resource_type = resource_type
        super().__init__(message)


class RollbackError(AtlantideError):
    """A compensation could not complete after a failed apply.

    A compensation is a provider call followed by a state write, so a partial one
    leaves state describing a resource that is no longer there; the stored hash
    still matches config, so the next plan reports NOOP. Raised alongside the
    original failure, not instead of it.
    """

    def __init__(self, node_id: str, reason: str) -> None:
        self.node_id = node_id
        self.op = "rollback"
        super().__init__(f"rollback of {node_id!r} did not complete: {reason}")


class StateError(AtlantideError):
    """State backend operation failed."""


class SecretsError(AtlantideError):
    """Sealing/unsealing failed (unknown backend, bad key, corrupt ciphertext)."""


class LockError(AtlantideError):
    """State lock could not be acquired or released."""


class LeaseLostError(LockError):
    """The state lock stopped being held part-way through a run.

    Distinct from :class:`LockError`, which means a run never started. This one
    means a run *did* start, wrote to the provider, and then found its lease
    taken by someone else — so another run may now be acting on the same
    resources. Nothing is rolled back: a compensation is itself a write, and a
    run that no longer holds the lock must not make one.

    The state store is therefore behind what exists at the provider. Recovery is
    ``atlantide refresh`` before the next apply.
    """


class InterruptedRunError(AtlantideError):
    """The operator interrupted a run (Ctrl-C).

    Not a failure of the infrastructure, so it renders and exits differently: the
    conventional 130 rather than 1, and without the "error:" framing that implies
    something went wrong. Completed nodes are compensated on the way out where the
    run still held its lock.
    """


class FencedWriteError(StateError):
    """A state write was refused because the writer no longer holds the lock.

    The store, not the writer, decides this — which is what makes it different
    from :class:`LeaseLostError`. A run whose local clock still believes its lease
    is good can be wrong; a conditional write against the recorded holder cannot.
    It is the last line between two concurrent runs and a silently merged state.
    """


class PlanDriftError(AtlantideError):
    """The changeset about to run is not the one that was approved.

    An apply re-diffs once it holds the state lock — it must, or a resource
    another run created in the meantime would still be planned as a CREATE and
    get built twice. So the plan a human read and the plan that executes can
    differ, and the gap between them is exactly where an unreviewed destroy fits.
    Raised rather than reconciled: which of the two is wanted is the operator's
    call, not the engine's.
    """


class PreventDestroyError(AtlantideError):
    """A planned destroy hit a resource with ``prevent_destroy`` set."""


class PolicyConfigError(AtlantideError):
    """A policy binding passes arguments the policy cannot use."""


class PolicyViolationError(AtlantideError):
    """One or more mandatory policies failed; the apply is blocked."""

    def __init__(self, summary: str, violations: list[object] | None = None) -> None:
        self.violations = violations or []
        super().__init__(summary)
