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
from typing import Any, ClassVar, override

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError, field_validator
from pydantic_core import InitErrorDetails
from pydantic_core.core_schema import ValidatorFunctionWrapHandler
from returns.result import Failure, Result, Success

from atlantide.core._component_prefix import active_prefix as _component_prefix
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
from atlantide.core.types import HANDLES, UNSET, Ref, StackOutputRef

#: Shared by every resource that declares no lifecycle; ``Lifecycle`` is frozen.
_DEFAULT_LIFECYCLE = Lifecycle()


class Resource(BaseModel):
    """Base class for all managed resources."""

    model_config = ConfigDict(extra="forbid")

    class Meta:
        provider: ClassVar[str] = ""

    _logical_name: str = PrivateAttr()
    _stack: str = PrivateAttr()
    #: ``None`` means no overrides and reads back as ``_DEFAULT_LIFECYCLE``. Not
    #: ``default_factory=Lifecycle``: pydantic inspects a factory's signature on
    #: every instantiation.
    _lifecycle: Lifecycle | None = PrivateAttr(default=None)
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
        not expressed through a value. Reading ``other.arn`` already creates an
        edge; ``depends_on`` covers cases where nothing is read, such as an IAM
        policy that must propagate before its consumer starts.

        Entries are resources or node ids. The edge affects ordering only: it is
        excluded from the content hash, so adding one does not re-plan its targets.
        """
        require_identifier(name, "resource")
        # Namespace under the enclosing component so a component instantiated
        # twice does not collide on node ids.
        prefix = _component_prefix()
        if prefix is not None:
            name = f"{prefix}-{name}"
        _apply_stack_defaults(type(self), name, data)
        super().__init__(**data)
        self._logical_name = name
        self._stack = current_stack()
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
        value: tuple[str, ...] = self._private("_depends_on")
        return value

    @field_validator("*", mode="wrap")
    @classmethod
    def _allow_refs_and_unset(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Let Ref, SecretRef, StackOutputRef, Transform, and UNSET pass through any typed field.

        A value containing a live handle at any depth (for example a
        ``StackReference`` output inside a ``tags`` dict) is validated everywhere
        except where a handle stands; see :func:`_validate_unless_handle`.
        """
        return _validate_unless_handle(value, handler)

    @property
    def logical_name(self) -> str:
        value: str = self._private("_logical_name")
        return value

    @property
    def stack(self) -> str:
        value: str = self._private("_stack")
        return value

    @property
    def lifecycle(self) -> Lifecycle:
        lifecycle: Lifecycle | None = self._private("_lifecycle")
        return _DEFAULT_LIFECYCLE if lifecycle is None else lifecycle

    def _private(self, name: str) -> Any:
        """A private attribute, read directly from pydantic's private store.

        Private attributes are not in the instance ``__dict__``, so ordinary lookup
        raises ``AttributeError`` internally before pydantic's ``__getattr__`` finds
        the value. These accessors run on every ``node_id`` access. Names not in the
        store fall back to ordinary lookup.
        """
        private = self.__pydantic_private__
        if private is not None and name in private:
            return private[name]
        return getattr(self, name)

    @classmethod
    def provider_name(cls) -> str:
        return getattr(cls.Meta, "provider", "")

    @classmethod
    def type_name(cls) -> str:
        provider = cls.provider_name()
        return f"{provider}.{cls.__name__}" if provider else cls.__name__

    @property
    def node_id(self) -> str:
        private = self.__pydantic_private__
        if private is not None and "_stack" in private and "_logical_name" in private:
            return format_node_id(private["_stack"], self.type_name(), private["_logical_name"])
        return format_node_id(self._stack, self.type_name(), self._logical_name)

    @override
    def __getattribute__(self, item: str) -> Any:
        if item[:1] == "_":
            # Pydantic never makes an underscore name a field, so the UNSET->Ref
            # rewrite below cannot apply.
            return super().__getattribute__(item)
        value = super().__getattribute__(item)
        if value is UNSET and item in type(self).model_fields:
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

    UNSET and a bare handle pass through unvalidated. A value with a handle
    nested inside it is validated as written, and only errors whose input holds a
    handle are dropped: ``{"k": ref, "n": 7}`` on a ``dict[str, str]`` still
    rejects the ``7``. The check is all it does: the value is stored exactly as
    written (a route dict stays a dict, no defaults filled in), so its canonical
    form and hash match what it had before handle-bearing values were checked.
    Nothing re-validates the field once its handles resolve at apply.
    """
    if value is UNSET or isinstance(value, HANDLES):
        return value
    if not contains_handle(value):
        return handler(value)
    try:
        handler(value)
    except ValidationError as exc:
        # An unknown key is wrong whatever its value, so `extra_forbidden` (whose
        # input is the value) is never excused by a handle.
        real = [
            err
            for err in exc.errors()
            if err["type"] == "extra_forbidden" or not contains_handle(err["input"])
        ]
        if real:
            raise _narrowed(exc, real) from None
    return value


def _narrowed(exc: ValidationError, errors: list[Any]) -> ValidationError:
    """``exc`` restricted to ``errors``, so a handle is not reported as a type error.

    Falls back to ``exc`` whole for an error type pydantic cannot rebuild (a
    validator's custom error).
    """
    details: list[InitErrorDetails] = [
        {
            "type": err["type"],
            "loc": err["loc"],
            "input": err["input"],
            **({"ctx": err["ctx"]} if "ctx" in err else {}),  # type: ignore[typeddict-item]
        }
        for err in errors
    ]
    try:
        return ValidationError.from_exception_data(exc.title, details)
    except KeyError:
        return exc


def _apply_stack_defaults(cls: type[Resource], name: str, data: dict[str, Any]) -> None:
    """Inject stack-scoped defaults into ``data`` before pydantic validation.

    - ``region``: the active stack's region, when the resource has that field and
      the caller did not pass one.
    - physical name: when a stack ``name_prefix`` is active and the marked name
      field is omitted, compose it as ``{prefix}-{logical-name}-{stack}``.
    - ``tags``: the active stack's tags, merged under the resource's own.

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
    _merge_stack_tags(cls, data)


def _merge_stack_tags(cls: type[Resource], data: dict[str, Any]) -> None:
    """Merge active stack tags under the resource's own ``tags`` (own wins).

    Runs before validation, so a stack tag of the wrong type is rejected by the
    field's own type like any other value.
    """
    stack_tags = current_stack_tags()
    field = cls.model_fields.get("tags")
    if not stack_tags or field is None:
        return
    own = data["tags"] if "tags" in data else field.get_default(call_default_factory=True)
    if own is UNSET:
        # A computed `tags` field is provider output; overwriting UNSET would
        # replace its Ref with a literal.
        return
    if own is not None and not isinstance(own, dict):
        # Replacing a non-dict here would discard the declared value, dropping
        # the property from the IR and its edge from the graph.
        raise IRError(
            f"{cls.__name__}.tags must be a dict to merge with the stack's "
            f"tags, got {type(own).__name__} — a Ref or Transform cannot be merged "
            "at config time; build the full mapping yourself"
        )
    data["tags"] = {**stack_tags, **own} if isinstance(own, dict) else stack_tags


def output(name: str, value: Any) -> StackOutputRef:
    """Export ``value`` (a literal or a resource ``Ref``) under ``name``.

    Recorded into the active registry, namespaced by the current stack; must be
    called during config evaluation. The returned handle is equivalent to
    ``StackReference(<this stack>).output(name)`` and lets a later stack in the
    same config consume the export; it is inlined into a dependency edge (see
    :func:`atlantide.core.inline.inline_stack_outputs`). A stack in a separate
    config uses :class:`StackReference`, resolved from committed state at apply.
    """
    registry = active_registry()
    if registry is None:
        raise RegistryError("output() must be called during config evaluation")
    registry.add_output(f"{current_stack()}:{name}", value)
    return StackOutputRef(current_stack(), name)


class Nested(BaseModel):
    """Base for a structured value inside a resource field.

    For example a security-group rule, a route, or an alias target. Differences
    from a plain ``BaseModel``:

    * a field may hold a :class:`~atlantide.core.types.Ref` (``Route(gateway_id=
      igw.internet_gateway_id)``), which pydantic would otherwise reject;
    * unknown keys are refused, so a typo in a nested field raises.

    The tree walkers find Refs inside a ``Nested``, so dependency edges form as
    they do from a top-level field.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="wrap")
    @classmethod
    def _allow_refs(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """Same rule as :meth:`Resource._allow_refs_and_unset`; see there."""
        return _validate_unless_handle(value, handler)


class DataSource(Resource):
    """A read-only lookup of an object that exists already and is not managed here.

    A :class:`Resource` whose create and update are reads and whose delete is a
    no-op (``providers/local``'s ``SourceFile`` is one). Consequently:

    * inputs are the query and are immutable; outputs are what was found;
    * the value is read once at apply and pinned in state, so a plan performs no
      provider I/O and two runs of one config produce identical IR;
    * ``destroy`` drops the state row without a provider call, since atlantide
      did not create the object.

    Re-reading on every plan (as a latest-AMI lookup needs) is not supported: it
    breaks plan determinism.
    """


class ResourceRegistry:
    """Collects the resources declared during one config evaluation."""

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._policy_bindings: list[PolicyBinding] = []
        self._outputs: dict[str, Any] = {}
        #: The config inputs this evaluation read (see `ConfigAPI.input`).
        self.inputs: dict[str, Any] = {}
        #: Every environment a `Config` in this evaluation declared, and the subset
        #: `--env` selected. The planner uses both to distinguish an unselected
        #: environment from one the config no longer declares.
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

    A bare string is rejected rather than iterated into single-character edges,
    as ``Lifecycle.aliases`` does.
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
