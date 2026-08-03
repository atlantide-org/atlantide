"""Resource base class and the per-evaluation resource registry.

A ``Resource`` is a typed pydantic model whose fields carry mutability metadata
(see :mod:`atlantide.core.fields`). Instances are identified by a logical name
and auto-register into the active :class:`ResourceRegistry` while config
evaluates. Reading a provider-computed field before apply yields a
:class:`~atlantide.core.types.Ref`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator
from pydantic_core.core_schema import ValidatorFunctionWrapHandler
from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.component import current_component_prefix
from atlantide.core.errors import IRError, RegistryError
from atlantide.core.fields import Mutability, field_mutability, physical_name_field
from atlantide.core.lifecycle import Lifecycle
from atlantide.core.markers import canonicalize, collect_refs, contains_handle
from atlantide.core.node_id import format_node_id, require_identifier, require_sequence
from atlantide.core.policy import PolicyBinding
from atlantide.core.stack import (
    current_stack,
    current_stack_name_prefix,
    current_stack_region,
    current_stack_tags,
)
from atlantide.core.types import Ref, StackOutputRef, _Unset


class Resource(BaseModel):
    """Base class for all managed resources."""

    model_config = ConfigDict(extra="forbid")

    class Meta:
        provider: ClassVar[str] = ""

    _logical_name: str = PrivateAttr()
    _stack: str = PrivateAttr()
    _lifecycle: Lifecycle = PrivateAttr(default_factory=Lifecycle)
    #: Explicit ordering edges, as node ids. See ``depends_on`` in ``__init__``.
    _depends_on: tuple[str, ...] = PrivateAttr(default=())

    def __init__(
        self,
        name: str,
        /,
        *,
        lifecycle: Lifecycle | None = None,
        depends_on: Sequence[Resource | str] = (),
        **data: Any,
    ) -> None:
        """Declare a resource.

        ``depends_on`` orders this resource after others when the dependency is
        real but not expressible as a value. Most ordering needs no declaring —
        reading ``other.arn`` already creates the edge — so reach for this only
        when nothing is read: an IAM policy that must propagate before the thing
        using it starts, a bucket policy that must exist before an upload.

        Pass the resources themselves (or their node ids). The edge orders and
        nothing more: it is deliberately excluded from the content hash, so
        adding one never re-plans the resources it points at.
        """
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
        self._depends_on = _explicit_edges(depends_on)
        registry = active_registry()
        if registry is not None:
            # Unwrap the registration Result: constructors raise.
            outcome = registry.register(self)
            if isinstance(outcome, Failure):
                raise outcome.failure()

    @property
    def depends_on(self) -> tuple[str, ...]:
        """Explicitly declared ordering edges, as node ids."""
        return self._depends_on

    @field_validator("*", mode="wrap")
    @classmethod
    def _allow_refs_and_unset(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Let Ref, SecretRef, StackOutputRef, and UNSET pass through any typed field.

        A value containing *any* live handle anywhere (even nested — a
        ``StackReference`` output inside a ``tags`` dict, a ``Transform`` in an
        ``env`` mapping) also skips validation here; it is re-validated at apply
        time once the handle resolves. Testing only for nested ``Ref`` rejected
        the other handle types in exactly the nested positions the inline
        machinery exists to support.
        """
        return _validate_unless_handle(value, handler)

    def _apply_stack_tags(self) -> None:
        """Merge active stack tags under this resource's own ``tags`` (own wins)."""
        stack_tags = current_stack_tags()
        if not stack_tags or "tags" not in type(self).model_fields:
            return
        # The raw stored value, not `getattr`: `__getattribute__` turns a stored
        # UNSET into a Ref, which would send a computed `tags` field into the
        # non-dict arm below instead of being tolerated.
        own = self.__dict__.get("tags")
        if isinstance(own, _Unset):
            # A computed `tags` field is provider-owned output: there is nothing
            # to merge at config time, and writing the stack tags over UNSET
            # would hand back a literal where a Ref belongs.
            return
        if own is not None and not isinstance(own, dict):
            # Runs after `super().__init__`, so replacing a non-dict would discard
            # the declared value before `input_values()` sees it, dropping the
            # property from the IR and its edge from the graph.
            raise IRError(
                f"{type(self).__name__}.tags must be a dict to merge with the stack's "
                f"tags, got {type(own).__name__} — a Ref or Transform cannot be merged "
                "at config time; build the full mapping yourself"
            )
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

    @override
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
            name: raw[name] for name, mut in mutability.items() if mut is not Mutability.COMPUTED
        }

    def canonical_inputs(self) -> dict[str, Any]:
        """JSON-safe inputs with Refs in stable ``{"$ref": ...}`` form."""
        return {name: canonicalize(value) for name, value in self.input_values().items()}

    def refs(self) -> list[Ref]:
        """Every Ref reachable from this resource's input fields."""
        return [ref for value in self.input_values().values() for ref in collect_refs(value)]


def _validate_unless_handle(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """The shared wrap-validator body for ``Resource`` and ``Nested``.

    UNSET and anything containing a live handle skip validation now and are
    re-validated at apply once the handle resolves.
    """
    if isinstance(value, _Unset) or contains_handle(value):
        return value
    return handler(value)


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


class Nested(BaseModel):
    """Base for a structured value inside a resource field.

    A security-group rule, a route, an alias target: things with a shape worth
    typing, which are not resources of their own. Two behaviours they need and a
    plain ``BaseModel`` does not have:

    * a field may hold a :class:`~atlantide.core.types.Ref` — ``Route(gateway_id=
      igw.internet_gateway_id)`` is the whole point of the type, and pydantic
      would otherwise reject it as "not a string";
    * unknown keys are refused, so a typo in a nested field is caught rather than
      silently ignored.

    Refs inside one are found by the tree walkers, so the dependency edge forms
    exactly as it would from a top-level field.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="wrap")
    @classmethod
    def _allow_refs(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Same rule as :meth:`Resource._allow_refs_and_unset`; see there."""
        return _validate_unless_handle(value, handler)


class DataSource(Resource):
    """A read-only lookup: something that exists already and is not managed here.

    Deliberately a :class:`Resource` subclass rather than a fifth method on the
    provider ABC. A data source *is* a resource whose create and update are reads
    and whose delete is nothing — ``providers/local``'s ``SourceFile`` already was
    one, hand-rolled. Forking the executor, the diff, the state model and the lock
    to express that would buy nothing the type flag does not.

    What follows from the subclassing:

    * inputs are the query and are immutable; outputs are what was found;
    * the value is read once at apply and pinned in state, so a plan performs no
      provider I/O and two runs of one config still produce identical IR;
    * it is never destroyed — ``destroy`` drops the row without calling anyone,
      because atlantide did not create the thing and must not remove it.

    The re-read-on-every-plan tier that a *latest AMI* lookup needs is where the
    determinism budget gets spent, and is deliberately not here yet: a half-wired
    flag in the public model is worse than an absent one.
    """


class ResourceRegistry:
    """Collects the resources declared during one config evaluation."""

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._policy_bindings: list[PolicyBinding] = []
        self._outputs: dict[str, Any] = {}
        #: The config inputs this evaluation actually read (see `ConfigAPI.input`).
        self.inputs: dict[str, Any] = {}
        #: Every environment a `Config` in this evaluation declared, and the
        #: subset `--env` selected. The planner needs both to tell a
        #: declared-but-unselected environment from one the config dropped.
        self.envs_declared: tuple[str, ...] = ()
        self.envs_selected: tuple[str, ...] = ()

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


def _explicit_edges(declared: Sequence[Resource | str]) -> tuple[str, ...]:
    """Normalise ``depends_on=`` to node ids.

    A bare string is rejected rather than iterated: ``depends_on="a"`` would
    otherwise become three single-character edges, which is the same trap
    ``Lifecycle.aliases`` guards against.
    """
    require_sequence(
        declared,
        "depends_on must be a sequence, not a bare string",
        f"write depends_on=[{declared!r}]",
        exc=IRError,
    )
    edges: set[str] = set()
    for item in declared:
        if isinstance(item, Resource):
            edges.add(item.node_id)
        elif isinstance(item, str):
            edges.add(item)
        else:
            raise IRError(f"depends_on takes resources or node ids, not {type(item).__name__}")
    return tuple(sorted(edges))
