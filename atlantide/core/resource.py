"""Resource base class and the per-evaluation resource registry.

A ``Resource`` is a typed pydantic model whose fields carry mutability metadata
(see :mod:`atlantide.core.fields`). Instances are identified by a logical name
and auto-register into the active :class:`ResourceRegistry` while config
evaluates. Reading a provider-computed field before apply yields a
:class:`~atlantide.core.types.Ref`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator
from pydantic_core.core_schema import ValidatorFunctionWrapHandler
from returns.result import Failure, Result, Success

from atlantide.core.component import current_component_prefix
from atlantide.core.errors import RegistryError
from atlantide.core.fields import Mutability, field_mutability, physical_name_field
from atlantide.core.lifecycle import Lifecycle
from atlantide.core.markers import canonicalize, collect_refs, contains_ref
from atlantide.core.node_id import format_node_id, require_identifier
from atlantide.core.policy import PolicyBinding
from atlantide.core.stack import (
    current_stack,
    current_stack_name_prefix,
    current_stack_region,
    current_stack_tags,
)
from atlantide.core.types import Ref, SecretRef, StackOutputRef, Transform, _Unset


class Resource(BaseModel):
    """Base class for all managed resources."""

    model_config = ConfigDict(extra="forbid")

    class Meta:
        provider: ClassVar[str] = ""

    _logical_name: str = PrivateAttr()
    _stack: str = PrivateAttr()
    _lifecycle: Lifecycle = PrivateAttr(default_factory=Lifecycle)

    def __init__(self, name: str, /, *, lifecycle: Lifecycle | None = None, **data: Any) -> None:
        require_identifier(name, "resource")
        # Namespace the logical name under any enclosing component (deterministic),
        # so a component instantiated twice does not collide on node ids.
        prefix = current_component_prefix()
        if prefix is not None:
            name = f"{prefix}-{name}"
        _apply_stack_defaults(type(self), name, data)  # region + name-prefix, before validation
        super().__init__(**data)
        self._logical_name = name
        self._stack = current_stack()
        self._apply_stack_tags()
        if lifecycle is not None:
            self._lifecycle = lifecycle
        registry = active_registry()
        if registry is not None:
            # Unwrap the registration Result: constructors raise.
            outcome = registry.register(self)
            if isinstance(outcome, Failure):
                raise outcome.failure()

    @field_validator("*", mode="wrap")
    @classmethod
    def _allow_refs_and_unset(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Let Ref, SecretRef, StackOutputRef, and UNSET pass through any typed field.

        A value containing a Ref anywhere (even nested) also skips validation
        here; it is re-validated at apply time once the handle resolves.
        """
        if isinstance(value, _Unset | SecretRef | StackOutputRef | Transform) or contains_ref(
            value
        ):
            return value
        return handler(value)

    def _apply_stack_tags(self) -> None:
        """Merge active stack tags under this resource's own ``tags`` (own wins)."""
        stack_tags = current_stack_tags()
        if not stack_tags or "tags" not in type(self).model_fields:
            return
        own = getattr(self, "tags", None)
        merged = {**stack_tags, **own} if isinstance(own, dict) else dict(stack_tags)
        setattr(self, "tags", merged)  # noqa: B010 - dynamic field name

    @property
    def logical_name(self) -> str:
        return self._logical_name

    @property
    def stack(self) -> str:
        return self._stack

    @property
    def lifecycle(self) -> Lifecycle:
        return self._lifecycle

    @classmethod
    def provider_name(cls) -> str:
        return getattr(cls.Meta, "provider", "")

    @classmethod
    def type_name(cls) -> str:
        provider = cls.provider_name()
        return f"{provider}.{cls.__name__}" if provider else cls.__name__

    @property
    def node_id(self) -> str:
        return format_node_id(self._stack, self.type_name(), self._logical_name)

    def __getattribute__(self, item: str) -> Any:
        value = super().__getattribute__(item)
        if isinstance(value, _Unset) and item in type(self).model_fields:
            return Ref(node_id=self.node_id, attr=item)
        return value

    def input_values(self) -> dict[str, Any]:
        """Raw values of all non-computed fields (Refs kept as Ref objects)."""
        mutability = field_mutability(type(self))
        raw = self.__dict__
        return {
            name: raw[name]
            for name, mut in mutability.items()
            if mut is not Mutability.COMPUTED
        }

    def canonical_inputs(self) -> dict[str, Any]:
        """JSON-safe inputs with Refs in stable ``{"$ref": ...}`` form."""
        return {name: canonicalize(value) for name, value in self.input_values().items()}

    def refs(self) -> list[Ref]:
        """Every Ref reachable from this resource's input fields."""
        found: list[Ref] = []
        for value in self.input_values().values():
            found.extend(collect_refs(value))
        return found


def _apply_stack_defaults(cls: type[Resource], name: str, data: dict[str, Any]) -> None:
    """Inject stack-scoped defaults into ``data`` before pydantic validation.

    - ``region``: the active stack's region, when the resource has that field and
      the caller did not pass one.
    - physical name: when a stack ``name_prefix`` is active and the marked name
      field is omitted, compose it as ``{prefix}-{logical-name}-{stack}``.

    An explicit value always wins.
    """
    fields = cls.model_fields
    region = current_stack_region()
    if region is not None and "region" in fields and "region" not in data:
        data["region"] = region
    prefix = current_stack_name_prefix()
    if prefix is not None:
        field = physical_name_field(cls)
        if field is not None and field not in data:
            data[field] = f"{prefix}-{name}-{current_stack()}"


def output(name: str, value: Any) -> StackOutputRef:
    """Export ``value`` (a literal or a resource ``Ref``) under ``name``.

    Recorded into the active registry, namespaced by the current stack. Must be
    called during config evaluation. Returns a handle to the export so a later
    stack in the *same* config can consume it without repeating the name — it is
    exactly ``StackReference(<this stack>).output(name)``, and is inlined into a
    real dependency edge (see :func:`atlantide.core.inline.inline_stack_outputs`).
    A stack applied by a *separate* config must still name it via
    :class:`StackReference` (resolved from committed state at apply).
    """
    registry = active_registry()
    if registry is None:
        raise RegistryError("output() must be called during config evaluation")
    registry.add_output(f"{current_stack()}:{name}", value)
    return StackOutputRef(current_stack(), name)


class ResourceRegistry:
    """Collects the resources declared during one config evaluation."""

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._policy_bindings: list[PolicyBinding] = []
        self._outputs: dict[str, Any] = {}

    def add_policy_binding(self, binding: PolicyBinding) -> None:
        """Record a config-declared policy binding (see ``atlantide.policy.enforce``)."""
        self._policy_bindings.append(binding)

    @property
    def policy_bindings(self) -> tuple[PolicyBinding, ...]:
        return tuple(self._policy_bindings)

    def add_output(self, key: str, value: Any) -> None:
        """Record a config-declared output (see ``atlantide.core.output``)."""
        if key in self._outputs:
            raise RegistryError(f"duplicate output {key!r}")
        self._outputs[key] = value

    @property
    def outputs(self) -> dict[str, Any]:
        """Declared exports, keyed ``{stack}:{name}`` (deterministic order)."""
        return dict(self._outputs)

    def register(self, resource: Resource) -> Result[None, RegistryError]:
        node_id = resource.node_id
        if node_id in self._resources:
            return Failure(RegistryError(f"duplicate resource {node_id!r}"))
        self._resources[node_id] = resource
        return Success(None)

    def get(self, node_id: str) -> Result[Resource, RegistryError]:
        resource = self._resources.get(node_id)
        if resource is None:
            return Failure(RegistryError(f"unknown resource {node_id!r}"))
        return Success(resource)

    def all(self) -> list[Resource]:
        """Deterministic (node_id-sorted) list of registered resources."""
        return [self._resources[k] for k in sorted(self._resources)]

    def __len__(self) -> int:
        return len(self._resources)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._resources


_current: ContextVar[ResourceRegistry | None] = ContextVar("atlantide_registry", default=None)


def active_registry() -> ResourceRegistry | None:
    return _current.get()


@contextmanager
def collecting() -> Iterator[ResourceRegistry]:
    """Activate a fresh registry; resources created inside auto-register."""
    registry = ResourceRegistry()
    token = _current.set(registry)
    try:
        yield registry
    finally:
        _current.reset(token)
