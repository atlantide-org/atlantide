"""The typed environment matrix: one ``Config`` holding every environment.

A config declares its environments and what differs between them once: an
:class:`EnvSchema` subclass (or a mapping of :func:`var` declarations) plus
``Config(AppEnv, envs={"dev": {...}, "prod": {...}})``. ``config.envs()`` then
yields each environment as an object whose variables are ordinary attributes.
The user guide lives in the docs site's ``reference/authoring.md``.

A ``Config`` is the checked-in matrix, validated eagerly and identical for every
run; ``atlantide.input()`` is a per-run parameter. ``region``, ``tags`` and
``name_prefix`` are well-known keys every schema carries implicitly.

Import graph: this module imports only ``core._config_types``, ``core._describe``
and ``core.errors`` (``_config_types`` adds ``core.node_id``). ``core.stack``
imports it and ``core.resource`` imports ``core.stack``, so importing
``resource`` here would create a cycle.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast, overload, override

# Imported under private names: this module is config-importable, so its public
# namespace is the config surface.
from atlantide.core._config_types import MISSING as _MISSING
from atlantide.core._config_types import RESERVED as _RESERVED
from atlantide.core._config_types import SUPPORTED_TYPES as _SUPPORTED_TYPES
from atlantide.core._config_types import annotation_type as _annotation_type
from atlantide.core._config_types import describe_type_arg as _describe_type_arg
from atlantide.core._config_types import own_annotations as _own_annotations
from atlantide.core._config_types import require_default_matches as _require_default_matches
from atlantide.core._config_types import require_variable_name as _require_variable_name
from atlantide.core._config_types import resolve_envs as _resolve_envs
from atlantide.core._describe import describe_type, describe_value
from atlantide.core.errors import LanguageError

#: The types ``var()`` accepts, by name, as an ``EnvSchema`` annotation spells them.
#: The Atlas-lang validator checks annotations against this set without
#: evaluating them.
SUPPORTED_FIELD_TYPE_NAMES: frozenset[str] = frozenset(t.__name__ for t in _SUPPORTED_TYPES)

#: Keys every schema carries implicitly, so an environment can supply what
#: `Stack` needs. An explicit declaration in `schema=` takes precedence and may
#: make the key required.
_WELL_KNOWN: dict[str, type] = {"region": str, "tags": dict, "name_prefix": str}


@dataclass(frozen=True, slots=True)
class Var:
    """One declared environment variable: its type and optional default."""

    type: type
    default: Any = _MISSING
    #: Whether an environment may supply ``None``. ``default=None`` implies it;
    #: an ``X | None`` field with another default sets it explicitly.
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.default is None:
            object.__setattr__(self, "nullable", True)

    @property
    def required(self) -> bool:
        """Whether every environment must supply this variable."""
        return self.default is _MISSING

    @override
    def __repr__(self) -> str:
        # Renders as the `var(...)` call, not `default=<object object at 0x...>`.
        if self.required:
            return f"var({self.type.__name__})"
        if self.nullable and self.default is not None:
            # Only an `X | None = <value>` field; shown as it was annotated.
            return f"var({self.type.__name__} | None, default={self.default!r})"
        return f"var({self.type.__name__}, default={self.default!r})"


def var(type_: type, default: Any = _MISSING) -> Var:
    """Declare an environment variable, e.g. ``var(int, default=1)``.

    Without a ``default`` the variable is required: every environment must
    supply it, checked when the :class:`Config` is constructed.
    ``default=None`` makes it optional and nullable.
    """
    if type_ not in _SUPPORTED_TYPES:
        supported = ", ".join(t.__name__ for t in _SUPPORTED_TYPES)
        raise LanguageError(
            f"var() type must be one of {supported}, got {_describe_type_arg(type_)} — "
            f"parameterised generics such as list[str] are not supported"
        )
    if default is not _MISSING:
        _require_default_matches(f"var({type_.__name__})", type_, default)
    return Var(type=type_, default=default)


#: Names an environment variable may not take (the environment's own API), as
#: a set for the Atlas-lang validator's ``EnvSchema`` check.
RESERVED_FIELD_NAMES: frozenset[str] = frozenset(_RESERVED)


def _field_var(owner: str, field: str, type_: type, default: Any, *, nullable: bool = False) -> Var:
    """A declared field with a default, held to exactly what ``var()`` accepts.

    Without the check, ``field: int = 'x'`` would be stored and read as a str.
    """
    _require_default_matches(f"{owner}.{field}", type_, default)
    return Var(type=type_, default=default, nullable=nullable)


def check_field_default(owner: str, field: str, annotation: Any, default: Any) -> None:
    """Check one ``EnvSchema`` field's default the way class creation will.

    The interpreter calls it per field so a bad default is reported on its own
    line. ``__init_subclass__`` repeats the check at class creation, which
    covers ordinary Python.
    """
    type_, _ = _annotation_type(owner, field, annotation)
    _field_var(owner, field, type_, default)


class EnvSchema:
    """Base for one environment's resolved variables: ``env.name``, ``env.domain``.

    Subclass it to declare an environment's shape; the variables become
    annotated attributes that an editor completes and a type checker checks::

        class AppEnv(EnvSchema):
            region: str
            price_class: str = "PriceClass_100"

    Atlas-lang forbids ``class`` except for a subclass of this type whose body
    is only annotated fields (see :mod:`atlantide.lang.validate`), so a schema
    is data.

    ``__getattr__`` is hidden from type checkers, so only declared fields
    type-check. At runtime an unknown name raises an error listing the declared
    variables.

    Immutability is structural: Atlas-lang cannot assign to an attribute (the
    interpreter binds only ``Name``, ``Tuple``/``List`` and ``Subscript`` targets).
    """

    # Declared here so a subclass with `__slots__ = ()` carries no `__dict__`.
    __slots__ = ("_declared", "_values", "name")

    #: Set by `__init_subclass__` on a declared subclass: the fields it declared.
    __atlas_fields__: ClassVar[dict[str, Var]] = {}

    #: The environment's name, and the first segment of every node id in it.
    name: str

    @override
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Collect a declared subclass's annotated fields into ``__atlas_fields__``.

        Reads the annotations plus either ``__atlas_defaults__`` (what the
        interpreter builds, keeping defaults out of the class namespace) or
        plain class attributes (what ordinary Python writes). Running here
        rather than in ``atlantide.lang`` keeps the interpreter to a single
        ``type()`` call and gives a hand-written ``class X(EnvSchema)`` the same
        behaviour.
        """
        super().__init_subclass__(**kwargs)
        defaults: Mapping[str, Any] = cls.__dict__.get("__atlas_defaults__", {})
        # A parent schema's fields first, in MRO order, so a field redeclared
        # here overrides the inherited one.
        fields: dict[str, Var] = {}
        for base in reversed(cls.__mro__[1:]):
            fields.update(base.__dict__.get("__atlas_fields__", {}))
        for field, annotation in _own_annotations(cls).items():
            _require_variable_name(field, f"field of {cls.__name__!r}")
            type_, nullable = _annotation_type(cls.__name__, field, annotation)
            if field in defaults:
                fields[field] = _field_var(
                    cls.__name__, field, type_, defaults[field], nullable=nullable
                )
            elif field in cls.__dict__:
                fields[field] = _field_var(
                    cls.__name__, field, type_, cls.__dict__[field], nullable=nullable
                )
                # In ordinary Python, `price_class: str = "..."` leaves a class
                # attribute that normal lookup finds before `__getattr__`, so every
                # environment would read the default. Delete it so reads fall
                # through to the instance values.
                delattr(cls, field)
            else:
                fields[field] = Var(type=type_, default=None) if nullable else Var(type=type_)
        cls.__atlas_fields__ = fields

    def __init__(self, name: str, values: Mapping[str, Any], declared: Sequence[str]) -> None:
        """Built by :class:`Config`, which has already validated ``values``.

        Config files obtain environments from ``config.envs()`` instead.
        """
        self.name = name
        self._values = dict(values)
        self._declared = tuple(declared)

    def _variable(self, item: str) -> Any:
        """Read a declared variable, or raise an error listing the declared ones.

        Shared by both ``__getattr__`` definitions (:class:`EnvSchema` hides its
        own from type checkers, :class:`EnvView` does not) so a typo reports the
        same way. Reading ``self._values`` cannot recurse: an underscore name
        raises ``AttributeError`` first.
        """
        # An underscore name is never a variable (`_require_variable_name`
        # rejects them), so it is a protocol probe (`copy`, `pickle` and `rich`
        # ask for dunders that must raise `AttributeError`) or an unset slot
        # during construction.
        if item.startswith("_"):
            raise AttributeError(item)
        if item in self._values:
            return self._values[item]
        # Bypass normal lookup: on a half-built instance `self.name` would
        # re-enter here and raise RecursionError.
        name = object.__getattribute__(self, "name")
        raise LanguageError(
            f"environment {name!r} has no variable {item!r} — "
            f"declared: {', '.join(self._declared) or '(none)'}"
        )

    if not TYPE_CHECKING:
        # Hidden from type checkers: a visible `__getattr__` returning `Any` would
        # make every attribute type-check. `EnvView` re-declares it visibly for
        # the `var()` form.
        def __getattr__(self, item: str) -> Any:
            """A declared variable. Called only when normal attribute lookup fails."""
            return self._variable(item)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def get(self, key: str, default: Any = None) -> Any:
        """The variable's value, or ``default`` when the environment has no such key."""
        return self._values.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        """This environment's variables as a plain dict in sorted key order.

        An environment object is not JSON-encodable; this dict can be passed to a
        resource field, ``merge()`` or ``to_json()``.
        """
        return {key: self._values[key] for key in sorted(self._values)}

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {self.as_dict()!r})"


class EnvView(EnvSchema):
    """The environment view for a config that declared its schema with ``var()``.

    A ``var()`` schema's field names exist only at runtime, so this class
    re-declares the ``__getattr__`` that :class:`EnvSchema` hides; otherwise
    every ``env.<var>`` in a ``schema=`` config would be a type error. A typo is
    therefore caught only at runtime; an :class:`EnvSchema` subclass catches it
    at edit time.
    """

    __slots__ = ()

    def __getattr__(self, item: str) -> Any:
        return self._variable(item)


@dataclass
class EnvSelection:
    """The ``--env`` selection for one evaluation, and what a ``Config`` did with it.

    Held in a :class:`~contextvars.ContextVar`, like the resource registry:
    ``Config(...)`` is constructed inside the interpreter, so the selection must
    be in scope rather than passed as an argument.
    """

    #: What the run asked for. ``None`` means every environment; ``()`` means none.
    requested: tuple[str, ...] | None = None
    #: Every environment the config declared.
    declared: tuple[str, ...] = ()
    #: The declared environments ``requested`` selects, recorded when a
    #: ``Config`` claims the selection; ``Config.envs()`` yields exactly these.
    selected: tuple[str, ...] = ()
    #: Whether a ``Config`` claimed this selection, so ``--env`` against a config
    #: that declares none can be reported.
    consumed: bool = False

    def claim(self, declared: tuple[str, ...]) -> None:
        """Bind this run's selection to the ``Config`` that just declared ``declared``.

        Only one ``Config`` may claim it, since the selection is global to the run.
        """
        if self.consumed:
            raise LanguageError(
                "a config declares more than one Config() — the environment "
                "selection is global to the run, so a second one is ambiguous"
            )
        self.consumed = True
        self.declared = declared


_selection: ContextVar[EnvSelection | None] = ContextVar("atlantide_env_selection", default=None)


def current_selection() -> EnvSelection | None:
    return _selection.get()


@contextmanager
def selecting(requested: Sequence[str] | None) -> Iterator[EnvSelection]:
    """Activate an environment selection for the config evaluation in the body."""
    selection = EnvSelection(requested=None if requested is None else tuple(requested))
    token = _selection.set(selection)
    try:
        yield selection
    finally:
        _selection.reset(token)


#: ``EnvT`` is the environment type a `Config` yields: the user's `EnvSchema` subclass
#: if declared, otherwise `EnvView`. The constructor overload binds it as ``E``.
class Config[EnvT: EnvSchema]:
    """Every environment this system has, and what differs between them.

    The schema is either an :class:`EnvSchema` subclass (the typed form, where an
    editor completes ``env.<var>`` and a type checker flags typos) or a mapping of
    :func:`var` declarations::

        Config(AppEnv, envs={...})                       # typed
        Config(schema={"size": var(int, default=1)}, envs={...})

    Both forms take the same ``envs`` mapping and run the same validation; the
    class adds only static knowledge of the field set. Everything is checked at
    construction, so a prod-only type error fails ``atlantide validate`` rather
    than the prod apply.
    """

    __slots__ = ("_envs", "_view", "schema")

    @overload
    def __init__[E: EnvSchema](
        self: Config[E], schema: type[E], *, envs: Mapping[str, Mapping[str, Any]]
    ) -> None: ...

    @overload
    def __init__(
        self: Config[EnvView],
        schema: Mapping[str, Var] | None = ...,
        *,
        envs: Mapping[str, Mapping[str, Any]],
    ) -> None: ...

    def __init__(
        self,
        schema: type[EnvSchema] | Mapping[str, Var] | None = None,
        *,
        envs: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if isinstance(schema, type) and issubclass(schema, EnvSchema):
            self._view: type[EnvSchema] = schema
            declared: Mapping[str, Var] = schema.__atlas_fields__
        else:
            self._view = EnvView
            declared = schema or {}
        self.schema = _resolve_schema(declared)
        self._envs = _resolve_envs(self.schema, envs, self._view)
        if (selection := current_selection()) is not None:
            selection.claim(tuple(self._envs))
            # Recorded now, not only by `envs()`: a config that reads its
            # environments through `env()` alone would otherwise leave every
            # declared one excluded. Also reports a mistyped `--env` up front.
            selection.selected = self._chosen(selection.requested)

    def envs(self) -> list[EnvT]:
        """The selected environments, sorted by name.

        Returns a list: the interpreter iterates it in order, and that order is
        the stack declaration order.
        """
        selection = current_selection()
        chosen = self._chosen(selection.requested if selection is not None else None)
        if selection is not None:
            selection.selected = chosen
        return cast("list[EnvT]", [self._envs[name] for name in chosen])

    def _chosen(self, requested: tuple[str, ...] | None) -> tuple[str, ...]:
        """Declared names filtered by ``requested``; all of them when it is ``None``."""
        if requested is None:
            return tuple(self._envs)
        for name in requested:
            # An unknown name raises: an empty selection would run as a successful
            # no-op.
            if name not in self._envs:
                raise self._unknown(name)
        wanted = set(requested)
        return tuple(name for name in self._envs if name in wanted)

    def env(self, name: str) -> EnvT:
        """One environment by name, ignoring the ``--env`` selection.

        For reading a shared environment's values outside the ``envs()`` loop,
        such as a base stack every environment depends on.
        """
        if name not in self._envs:
            raise self._unknown(name)
        return cast("EnvT", self._envs[name])

    def _unknown(self, name: str) -> LanguageError:
        return LanguageError(
            f"unknown environment {describe_value(name)} — declared: {', '.join(self._envs)}"
        )

    def names(self) -> list[str]:
        """Every declared environment name, sorted."""
        return list(self._envs)

    @override
    def __repr__(self) -> str:
        return f"Config(envs={list(self._envs)!r})"


def _resolve_schema(schema: Mapping[str, Var]) -> dict[str, Var]:
    """The declared schema plus the well-known keys it did not declare itself.

    Returned in sorted key order, which keeps each environment's values and the
    "declared: ..." error text in a stable order.
    """
    for name, declaration in schema.items():
        if not isinstance(declaration, Var):
            raise LanguageError(
                f"schema entry {name!r} must be a var(...), got {describe_type(declaration)}"
            )
        _require_variable_name(name, "schema entry")
    resolved = dict(schema)
    for name, type_ in _WELL_KNOWN.items():
        resolved.setdefault(name, Var(type=type_, default=None))
    return {name: resolved[name] for name in sorted(resolved)}
