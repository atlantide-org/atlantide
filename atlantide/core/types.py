"""Core value types: lazy references and the UNSET sentinel."""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any, TypeVar, Union, override

from atlantide.core import _tree
from atlantide.core.errors import IRError, LanguageError
from atlantide.core.node_id import require_sequence

T = TypeVar("T")

# Keys of the single-key ``{"$...": ...}`` markers that handles serialize to.
# Defined in this low-level module because the layers that recognise them cannot
# import each other: ``core.markers`` owns the codec, ``secrets`` owns sealing,
# and ``core.logging`` redacts both without importing either.
REF_KEY = "$ref"
SECRET_REF_KEY = "$secret_ref"
STACK_OUTPUT_KEY = "$stack_output"
TRANSFORM_KEY = "$transform"
SEALED_KEY = "$sealed"

#: Markers whose payload is a secret and must never be logged or displayed.
SECRET_MARKER_KEYS = (SECRET_REF_KEY, SEALED_KEY)


def _reject_field(field_name: str) -> None:
    """Refuse a template field that is not a bare positional index (``{}``, ``{0}``)."""
    raise LanguageError(
        f"template field {field_name!r} is not a plain positional index; "
        "attribute and item access in a format template is not allowed"
    )


class _PositionalFormatter(string.Formatter):
    """``str.format`` restricted to bare positional substitution.

    A field name such as ``{0.__class__.__init__}`` walks a live object and can
    reach the interpreter's globals from a config-supplied template. Format specs
    and ``!r`` conversions stay allowed because they have no side effects.
    """

    @override
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        if not field_name.isdigit():
            _reject_field(field_name)
        index = int(field_name)
        if index >= len(args):
            raise LanguageError(
                f"template references {{{index}}} but only {len(args)} argument(s) were given"
            )
        return args[index], field_name


_FORMATTER = _PositionalFormatter()


def check_template(template: str) -> None:
    """Raise :class:`LanguageError` if ``template`` is not plain positional substitution.

    Rejects attribute or item access and mixed auto (``{}``) and manual (``{0}``)
    numbering. Checks without substituting, so a template is validated at config
    time before its arguments are known; otherwise ``vformat`` would raise a bare
    ``ValueError`` for mixed numbering at apply time.
    """
    auto = manual = False
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if field_name is None:
            continue
        if field_name == "":
            auto = True
        elif field_name.isdigit():
            manual = True
        else:
            _reject_field(field_name)
    if auto and manual:
        raise LanguageError(
            "template mixes automatic {} and manual {0} placeholder numbering; use one style"
        )


def format_template(template: str, *args: Any) -> str:
    """Substitute ``args`` into ``template``'s positional placeholders.

    Config-supplied format strings are evaluated through this, not ``str.format``;
    :mod:`atlantide.reconcile.resolve` uses it to reduce an :func:`interpolate`
    transform at apply time.
    """
    return _FORMATTER.vformat(template, args, {})


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

    @override
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
        return {REF_KEY: f"{self.node_id}#{self.attr}"}


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A named handle to an externally stored secret, not the secret value.

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
        return {SECRET_REF_KEY: {"name": self.name, "provider": self.provider}}


@dataclass(frozen=True, slots=True)
class StackOutputRef:
    """A reference to another stack's committed output, resolved at apply time.

    ``StackReference("prod").output("vpc_id")`` yields this handle; the engine
    resolves it from the referenced stack's persisted outputs in state. Not a
    :class:`Ref` subclass, so it never forms a within-graph dependency edge: the
    referenced stack is applied separately.
    """

    stack: str
    name: str

    def canonical(self) -> dict[str, str]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {STACK_OUTPUT_KEY: f"{self.stack}:{self.name}"}


def _canonical_arg(value: Any) -> Any:
    """Canonical form of one transform argument (handles -> markers, recursively).

    Recurses into dicts and nested models: a live ``Ref`` inside one
    (``concat("p-", {"k": other.arn})``) would otherwise fail hashing as not
    JSON-encodable.
    """
    return _tree.handles_to_markers(value, HANDLES)


@dataclass(frozen=True, slots=True)
class Transform:
    """A deferred, pure transform over values that are unknown until apply.

    The language is not re-run at apply, so a transform is serialized as data (an
    operation name plus arguments, which are literals or other handles), not a
    closure. Its ``$transform`` marker canonicalizes and hashes deterministically;
    the executor evaluates it from a fixed op allowlist once the wrapped ``Ref``s
    resolve. Build one with :func:`concat`, :func:`interpolate`, or :func:`join`.
    """

    op: str
    args: tuple[Any, ...]

    def canonical(self) -> dict[str, Any]:
        """Stable serialized form used in canonical inputs and the IR."""
        return {TRANSFORM_KEY: {"op": self.op, "args": [_canonical_arg(a) for a in self.args]}}

    @property
    def _atlas_operands(self) -> tuple[Any, ...]:
        """Children the tree walkers descend into (to find nested ``Ref``s)."""
        return self.args


def concat(*parts: Any) -> Transform:
    """Concatenate parts (each a literal or ``Ref``) into one string at apply."""
    return Transform("concat", tuple(parts))


def interpolate(template: str, *args: Any) -> Transform:
    """Fill ``{}`` placeholders in ``template`` with ``args`` at apply.

    Example: ``interpolate("{}/img/{}", dist.domain, key)``. The template is
    checked at config time, so a field addressing attributes fails the plan
    rather than the apply.
    """
    check_template(template)
    return Transform("interpolate", (template, *args))


def join(separator: str, parts: Any) -> Transform:
    """Join an iterable of parts with ``separator`` at apply.

    ``parts`` may itself be unresolved (a computed list field reads back as a
    ``Ref``), so iteration is deferred to apply for handles. A bare string is
    rejected because ``tuple("abc")`` would join its characters; ``Lifecycle``
    and ``depends_on`` apply the same check.
    """
    require_sequence(
        parts,
        f"join() parts must be an iterable of parts, not the string {parts!r}",
        "did you mean a list?",
        exc=IRError,
    )
    if isinstance(parts, HANDLES):
        return Transform("join", (separator, parts))
    return Transform("join", (separator, tuple(parts)))


#: The live handle objects that serialize to single-key ``$...`` markers. Codecs
#: and validators read this tuple so they stay in sync.
HANDLES = (Ref, SecretRef, StackOutputRef, Transform)


# ``Input[T]``: a field accepts either a concrete value or a Ref. A plain alias, not a
# ``type`` statement: pydantic field annotations need the real ``Union`` at runtime.
Input = Union[T, Ref]  # noqa: UP007 - Union spelling required for a generic alias
