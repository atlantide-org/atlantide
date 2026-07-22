"""Core value types: lazy references and the UNSET sentinel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, Union

T = TypeVar("T")


class _Unset:
    """Sentinel for provider-computed fields that have no value yet.

    A single instance (:data:`UNSET`) exists. Reading a resource attribute that
    holds it yields a :class:`Ref` instead.
    """

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class Ref:
    """Lazy handle to another node's attribute, resolved at apply time.

    Referencing ``bucket.arn`` before apply returns ``Ref(node_id=..., attr="arn")``.
    IR lowering turns these into dependency edges; the executor resolves them once
    the upstream node has applied.
    """

    node_id: str
    attr: str

    def canonical(self) -> dict[str, str]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {"$ref": f"{self.node_id}#{self.attr}"}


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A named handle to an externally-stored secret — never the value itself.

    A field set to ``SecretRef("app/signing-key")`` records only the *name* (and
    optionally which secrets provider). Source, IR, and state carry the handle;
    the plaintext is resolved from the configured secrets backend in-memory at
    apply time and never persisted. Not a :class:`Ref` subclass, so it never
    forms a dependency edge.
    """

    name: str
    provider: str | None = None

    def canonical(self) -> dict[str, Any]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {"$secret_ref": {"name": self.name, "provider": self.provider}}


@dataclass(frozen=True, slots=True)
class StackOutputRef:
    """A reference to another stack's committed output, resolved at apply time.

    ``StackReference("prod").output("vpc_id")`` yields this handle; the engine
    resolves it from the referenced stack's persisted outputs in state. Not a
    :class:`Ref` subclass, so it is never a within-graph dependency edge — the
    referenced stack is applied separately (its outputs already committed).
    """

    stack: str
    name: str

    def canonical(self) -> dict[str, str]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {"$stack_output": f"{self.stack}:{self.name}"}


def _canonical_arg(value: Any) -> Any:
    """Canonical form of one transform argument (handles -> markers, recursively)."""
    if isinstance(value, HANDLES):
        return value.canonical()
    if isinstance(value, list | tuple):
        return [_canonical_arg(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Transform:
    """A deferred, pure transform over values that are unknown until apply.

    The language is not re-run at apply, so a transform is serialized as **data**
    — an operation name plus arguments (literals or other handles) — never a
    closure. Its ``$transform`` marker canonicalizes and hashes deterministically;
    the executor evaluates it from a fixed op allowlist once the wrapped ``Ref``s
    resolve. Build one with :func:`concat`, :func:`interpolate`, or :func:`join`.
    """

    op: str
    args: tuple[Any, ...]

    def canonical(self) -> dict[str, Any]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {"$transform": {"op": self.op, "args": [_canonical_arg(a) for a in self.args]}}

    @property
    def _atlas_operands(self) -> tuple[Any, ...]:
        """Children the tree walkers descend into (to find nested ``Ref``s)."""
        return self.args


def concat(*parts: Any) -> Transform:
    """Concatenate parts (each a literal or ``Ref``) into one string at apply."""
    return Transform("concat", tuple(parts))


def interpolate(template: str, *args: Any) -> Transform:
    """Fill ``{}`` placeholders in ``template`` with ``args`` at apply
    (``interpolate("{}/img/{}", dist.domain, key)``)."""
    return Transform("interpolate", (template, *args))


def join(separator: str, parts: Any) -> Transform:
    """Join an iterable of parts with ``separator`` at apply."""
    return Transform("join", (separator, tuple(parts)))


#: The live handle objects that serialize to single-key ``$...`` markers. Codecs
#: and validators read this tuple so they stay in sync.
HANDLES = (Ref, SecretRef, StackOutputRef, Transform)


# ``Input[T]``: a field accepts either a concrete value or a Ref.
Input = Union[T, Ref]  # noqa: UP007 - Union spelling required for a generic alias
