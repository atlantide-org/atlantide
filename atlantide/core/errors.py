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

    Raised before lowering, since substitution would otherwise recurse without
    bound before the graph's cycle check runs. ``chain`` names the output keys
    involved, e.g. ``common:vpc_id -> dev:x -> common:vpc_id``.
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

    Optional context: ``node_id`` (resource), ``op`` (CRUD phase), and
    ``resource_type``.
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
    can leave state describing a resource that no longer exists while its stored
    hash still matches config (the next plan reports NOOP). Raised alongside the
    original failure.
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

    Unlike a plain :class:`LockError`, the run started and wrote to the provider
    before its lease was taken, so another run may be acting on the same
    resources. Nothing is rolled back: a compensation is a write, and a run
    without the lock must not write.

    State can therefore lag the provider; run ``atlantide refresh`` before the
    next apply.
    """


class InterruptedRunError(AtlantideError):
    """The operator interrupted a run (Ctrl-C).

    Exits with 130 rather than 1 and renders without the "error:" prefix.
    Completed nodes are compensated on exit if the run still holds its lock.
    """


class FencedWriteError(StateError):
    """A state write was refused because the writer no longer holds the lock.

    Unlike :class:`LeaseLostError`, the store decides this through a conditional
    write against the recorded holder, so it does not depend on the writer's local
    clock. Prevents two concurrent runs from merging state.
    """


class PlanDriftError(AtlantideError):
    """The changeset about to run is not the one that was approved.

    An apply re-diffs once it holds the state lock, so a resource another run
    created meanwhile is not created twice. The re-diffed plan can differ from the
    approved one, including by an unreviewed destroy, so the apply stops instead
    of reconciling.
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
