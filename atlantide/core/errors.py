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


class NonDeterministicError(AtlantideError):
    """Config reached for a non-deterministic capability."""


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


class StateError(AtlantideError):
    """State backend operation failed."""


class SecretsError(AtlantideError):
    """Sealing/unsealing failed (unknown backend, bad key, corrupt ciphertext)."""


class LockError(AtlantideError):
    """State lock could not be acquired or released."""


class PreventDestroyError(AtlantideError):
    """A planned destroy hit a resource with ``prevent_destroy`` set."""


class PolicyViolationError(AtlantideError):
    """One or more mandatory policies failed; the apply is blocked."""

    def __init__(self, summary: str, violations: list[object] | None = None) -> None:
        self.violations = violations or []
        super().__init__(summary)
