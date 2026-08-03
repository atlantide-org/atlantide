"""The typed environment matrix: one ``Config`` holding every environment.

A config file declares the environments its system has and what differs between
them, once, in one place::

    class AppEnv(EnvSchema):
        domain: str
        size: int = 1

    config = Config(
        AppEnv,
        envs={
            "dev":  {"region": "eu-north-1", "domain": "dev.x.io"},
            "prod": {"region": "us-east-1",  "domain": "x.io", "size": 5},
        },
    )

    for env in config.envs():           # env: AppEnv
        with Stack(env.name, config=env):
            S3Bucket("assets", versioning=env.size > 1)

Declaring the shape as an :class:`EnvSchema` subclass is what makes ``env.size``
an ordinary annotated attribute — one an editor completes and a checker
diagnoses. The schema may instead be a mapping of :func:`var` declarations
(``Config(schema={"size": var(int, default=1)}, envs=...)``); it validates
identically and is the right choice when the static side does not matter.

This is not ``atlantide.input()``. An input is a per-run parameter supplied from
outside the repository (a CI build number, a fork's name prefix) and is untyped;
a ``Config`` is the checked-in environment matrix, validated eagerly and
identical for every run. What differs between environments belongs here; what
differs between runs of one environment is an input. The two compose: an
environment's value may be built from an input.

``region``, ``tags`` and ``name_prefix`` are well-known keys every schema has
implicitly, so ``Stack(env.name, config=env)`` needs no separate ``region=``. A
schema may re-declare one to make it required or to narrow it.

Import-graph note: this module imports only ``core.errors`` and ``core.node_id``.
``core.stack`` imports it (for ``Stack(config=...)``) and ``core.resource``
imports ``core.stack``, so importing ``resource`` from here would cycle.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast, get_args, overload

from typing_extensions import override

from atlantide.core.errors import LanguageError
from atlantide.core.node_id import require_identifier

_MISSING = object()

#: The environment type a `Config` yields: the user's `EnvSchema` subclass when
#: one was declared, otherwise `EnvView`. `E` binds it from the argument in the
#: constructor overload; `EnvT` carries it to `envs()`/`env()`.
E = TypeVar("E", bound="EnvSchema")
EnvT = TypeVar("EnvT", bound="EnvSchema")

#: What a ``var()`` may be declared as. Parameterised generics (``list[str]``)
#: are refused: they evaluate to a ``types.GenericAlias`` that ``isinstance``
#: cannot test against, and element-type checking is out of scope.
_SUPPORTED_TYPES: tuple[type, ...] = (str, int, float, bool, list, dict)

#: Keys every schema carries implicitly, so an environment can supply what
#: `Stack` needs without the author restating them. An explicit declaration in
#: `schema=` wins (it may make one required, which these are not).
_WELL_KNOWN: dict[str, type] = {"region": str, "tags": dict, "name_prefix": str}


@dataclass(frozen=True, slots=True)
class Var:
    """One declared environment variable: its type and, maybe, its default."""

    type: type
    default: Any = _MISSING

    @property
    def required(self) -> bool:
        """Whether every environment must supply this variable."""
        return self.default is _MISSING

    @override
    def __repr__(self) -> str:
        # Mirrors how it was written, so a traceback or a `Config` dump reads
        # back as source instead of `default=<object object at 0x...>`.
        if self.required:
            return f"var({self.type.__name__})"
        return f"var({self.type.__name__}, default={self.default!r})"


def var(type_: type, default: Any = _MISSING) -> Var:
    """Declare an environment variable, e.g. ``var(int, default=1)``.

    Without a ``default`` the variable is required: every environment must
    supply it, checked when the :class:`Config` is constructed rather than
    wherever the value is eventually read. ``default=None`` instead makes it
    optional and nullable.
    """
    if type_ not in _SUPPORTED_TYPES:
        supported = ", ".join(t.__name__ for t in _SUPPORTED_TYPES)
        raise LanguageError(
            f"var() type must be one of {supported}, got {_describe_type(type_)} — "
            f"parameterised generics such as list[str] are not supported"
        )
    if default is not _MISSING and default is not None and not _type_matches(default, type_):
        raise LanguageError(
            f"var({type_.__name__}) default {default!r} is a {type(default).__name__}"
        )
    return Var(type=type_, default=default)


def _describe_type(value: Any) -> str:
    """A readable name for whatever was passed where a type was expected."""
    name = getattr(value, "__name__", None)
    return name if isinstance(name, str) else repr(value)


def _type_matches(value: Any, expected: type) -> bool:
    """``isinstance`` with the bool/int hole closed.

    ``isinstance(True, int)`` is ``True``, so a plain check would let
    ``var(int)`` accept ``size=True``. Same guard as the project-file reader.
    """
    if expected is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if expected is float:
        return isinstance(value, int | float)
    return isinstance(value, expected)


#: Names an environment variable may not take: they are the environment's own
#: API, so a variable sharing one would be unreachable — `env.name` would answer
#: the environment name while `env["name"]` answered the value.
_RESERVED = ("name", "get", "as_dict")


def _require_variable_name(name: str, what: str) -> None:
    """Reject a variable name that could not be read back as ``env.<name>``."""
    if not name.isidentifier() or name.startswith("_"):
        raise LanguageError(
            f"{what} {name!r} must be a plain identifier not starting with '_' — "
            f"it is read back as `env.{name}`"
        )
    if name in _RESERVED:
        raise LanguageError(
            f"{what} {name!r} collides with an environment's own API "
            f"({', '.join(_RESERVED)}) — pick another name"
        )


def _annotation_type(owner: str, field: str, annotation: Any) -> tuple[type, bool]:
    """The declared type of an annotated field, and whether it is nullable.

    Accepts one of the six supported types, or ``X | None``. Annotations arrive
    either as objects (ordinary Python) or as strings (under
    ``from __future__ import annotations``), so both spellings are handled here
    rather than by calling ``get_type_hints``, which would evaluate arbitrary
    names out of the declaring module.
    """
    parts = _annotation_parts(annotation)
    named = [part for part in parts if part != "None"]
    written = " | ".join(parts)
    if len(named) != 1:
        raise LanguageError(
            f"field {field!r} of {owner!r}: only `X | None` may be combined, got {written!r}"
        )
    by_name = {supported.__name__: supported for supported in _SUPPORTED_TYPES}
    if named[0] not in by_name:
        raise LanguageError(
            f"field {field!r} of {owner!r} must be one of {', '.join(by_name)}, "
            f"got {written!r} — parameterised generics such as list[str] are not supported"
        )
    return by_name[named[0]], "None" in parts


def _annotation_parts(annotation: Any) -> tuple[str, ...]:
    """An annotation as the names it is written from: ``str | None`` -> ``("str", "None")``.

    Names, not types: an annotation reaches here as a string under
    ``from __future__ import annotations`` and as an object otherwise, and
    resolving the string form would mean evaluating a name out of the declaring
    module. Comparing spellings needs neither.
    """
    if isinstance(annotation, str):
        return tuple(part.strip() for part in annotation.split("|"))
    if members := get_args(annotation):  # a union, e.g. `str | None`
        return tuple(_annotation_name(member) for member in members)
    return (_annotation_name(annotation),)


def _annotation_name(annotation: Any) -> str:
    """One annotation member's name, with ``NoneType`` spelled the way it is written."""
    if annotation is None or annotation is type(None):
        return "None"
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation)


class EnvSchema:
    """Base for one environment's resolved variables: ``env.name``, ``env.domain``.

    Subclass it to declare an environment's shape, and the variables become
    ordinary annotated attributes that an editor completes and a type checker
    diagnoses::

        class AppEnv(EnvSchema):
            region: str
            price_class: str = "PriceClass_100"

    Without a subclass, field names come from the schema at runtime, so no
    fixed-field type can express them: an editor offers nothing and ``env.typo``
    type-checks clean.

    Atlas-lang normally forbids ``class``; :mod:`atlantide.lang.validate` makes a
    narrow exception for a subclass of this type whose body is only annotated
    fields. There are no methods, decorators or metaclass, so a schema is data.

    ``__getattr__`` is hidden from type checkers (see below), so a subclass's
    declared fields are the only ones that type-check. At runtime every lookup
    still falls through here, so a typo is answered by name.

    No ``frozen=`` machinery: Atlas-lang cannot assign to an attribute at all
    (the interpreter binds ``Name``, ``Tuple``/``List`` and ``Subscript``
    targets and rejects everything else), so immutability is structural.
    """

    # The storage every environment shares, declared once here so a subclass can
    # be `__slots__ = ()` and still carry no `__dict__`.
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
        plain class attributes (what ordinary Python writes). Doing it here
        rather than in `atlantide.lang` keeps the interpreter to a single
        ``type()`` call and makes a hand-written ``class X(EnvSchema)`` behave
        identically.
        """
        super().__init_subclass__(**kwargs)
        # `EnvView` declares no annotations, so it falls through with no fields —
        # no special case needed for the one subclass that is not a user schema.
        defaults: Mapping[str, Any] = cls.__dict__.get("__atlas_defaults__", {})
        fields: dict[str, Var] = {}
        for field, annotation in cls.__dict__.get("__annotations__", {}).items():
            _require_variable_name(field, f"field of {cls.__name__!r}")
            type_, nullable = _annotation_type(cls.__name__, field, annotation)
            if field in defaults:
                fields[field] = Var(type=type_, default=defaults[field])
            elif field in cls.__dict__:
                fields[field] = Var(type=type_, default=cls.__dict__[field])
                # `price_class: str = "..."` in ordinary Python leaves a *class*
                # attribute, which normal lookup finds before `__getattr__` runs
                # — so every environment would read the default instead of its
                # own value. The default is captured above, so drop the
                # attribute and let reads fall through to the instance. (The
                # interpreter never creates one: it passes `__atlas_defaults__`
                # precisely to keep config values out of the class namespace.)
                delattr(cls, field)
            else:
                fields[field] = Var(type=type_, default=None) if nullable else Var(type=type_)
        cls.__atlas_fields__ = fields

    def __init__(self, name: str, values: Mapping[str, Any], declared: Sequence[str]) -> None:
        """Built by :class:`Config`, which has already validated ``values``.

        Not meant to be called from a config file — an environment comes out of
        ``config.envs()``, already checked against the schema.
        """
        self.name = name
        self._values = dict(values)
        self._declared = tuple(declared)

    def _variable(self, item: str) -> Any:
        """Read a declared variable, or say what this environment declares.

        The body of ``__getattr__``, as an ordinary method so both classes can
        call it: :class:`EnvSchema` hides its ``__getattr__`` from type checkers
        and :class:`EnvView` does not, but a typo must read the same either way.
        Being a real method is also what makes ``self._values`` below safe — an
        underscore name is answered with ``AttributeError`` rather than looping
        back through here.
        """
        # An underscore name is never a variable (`_require_variable_name`
        # rejects them), so it is either a protocol probe -- `copy`, `pickle` and
        # `rich` ask for dunders that must answer `AttributeError` -- or this
        # object mid-construction.
        if item.startswith("_"):
            raise AttributeError(item)
        if item in self._values:
            return self._values[item]
        # Read `name` the long way round: on a half-built instance `self.name`
        # would come back here and turn a typo into a RecursionError.
        name = object.__getattribute__(self, "name")
        raise LanguageError(
            f"environment {name!r} has no variable {item!r} — "
            f"declared: {', '.join(self._declared) or '(none)'}"
        )

    if not TYPE_CHECKING:
        # The guard is load-bearing: a visible `__getattr__` returning `Any`
        # makes every attribute valid, which is the gap a declared schema closes.
        # `EnvView` re-declares a visible one, keeping the `var()` path
        # permissive for configs that never declared a schema class.
        def __getattr__(self, item: str) -> Any:
            """A declared variable. Only called for names outside ``__slots__``."""
            return self._variable(item)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def get(self, key: str, default: Any = None) -> Any:
        """The variable's value, or ``default`` when the environment has no such key."""
        return self._values.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        """This environment's variables as plain sorted data.

        Passing an environment straight into a resource field fails at IR
        canonicalization as "not JSON-encodable"; this is the conversion, usable
        with ``merge()``/``to_json()`` and safe in a resource field.
        """
        return {key: self._values[key] for key in sorted(self._values)}

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {self.as_dict()!r})"


class EnvView(EnvSchema):
    """The environment view for a config that declared its schema with ``var()``.

    A ``var()`` schema's field names exist only at runtime, so no fixed-field
    type can express them; this class therefore re-declares the ``__getattr__``
    :class:`EnvSchema` hides. Without it, every ``env.<var>`` in a ``schema=``
    config would be a type error in the author's editor.

    The cost is that a typo is caught only when the config runs. Declaring an
    :class:`EnvSchema` subclass instead moves that to edit time.
    """

    __slots__ = ()

    def __getattr__(self, item: str) -> Any:
        return self._variable(item)


@dataclass
class EnvSelection:
    """The ``--env`` selection for one evaluation, and what a ``Config`` did with it.

    Lives in a :class:`~contextvars.ContextVar` for the same reason the resource
    registry does: a ``Config(...)`` literal is constructed inside the
    interpreter, so the selection must already be in scope rather than passed as
    an argument the config author would have to thread through.
    """

    #: What the run asked for. ``None`` means every environment; ``()`` means none.
    requested: tuple[str, ...] | None = None
    #: Every environment the config declared.
    declared: tuple[str, ...] = ()
    #: What ``Config.envs()`` actually yielded.
    selected: tuple[str, ...] = ()
    #: Whether a ``Config`` saw this selection, so a ``--env`` against a config
    #: that declares none can be reported instead of silently doing nothing.
    consumed: bool = False

    def claim(self, declared: tuple[str, ...]) -> None:
        """Bind this run's selection to the ``Config`` that just declared ``declared``.

        Only one may: the selection is global to the run, so a second ``Config``
        would leave ``--env prod`` with no single set of environments to name.
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


class Config(Generic[EnvT]):
    """Every environment this system has, and what differs between them.

    The schema comes either as an :class:`EnvSchema` subclass — the typed form,
    where an editor completes ``env.<var>`` and a checker flags a typo — or as a
    mapping of :func:`var` declarations::

        Config(AppEnv, envs={...})                       # typed
        Config(schema={"size": var(int, default=1)}, envs={...})

    Both take the same ``envs`` mapping and run the same validation; the class
    only adds static knowledge of the field set. Everything is checked when the
    ``Config`` is constructed — a prod-only type error fails ``atlantide
    validate`` in CI rather than at the moment prod is applied.
    """

    __slots__ = ("_envs", "_view", "schema")

    @overload
    def __init__(
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
        # Positional-or-keyword, so `Config(schema={...}, envs=...)` keeps working.
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

    def envs(self) -> list[EnvT]:
        """The selected environments, sorted by name.

        A list rather than a generator: the interpreter iterates lists in order,
        and this is the order the stacks are declared in.
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
            # A typo must not yield an empty selection, which would read as a
            # successful run that did everything asked of it.
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
        return LanguageError(f"unknown environment {name!r} — declared: {', '.join(self._envs)}")

    def names(self) -> list[str]:
        """Every declared environment name, sorted."""
        return list(self._envs)

    @override
    def __repr__(self) -> str:
        return f"Config(envs={list(self._envs)!r})"


def _resolve_schema(schema: Mapping[str, Var]) -> dict[str, Var]:
    """The declared schema plus the well-known keys it did not declare itself.

    Returned in sorted key order, which keeps ``EnvView.as_dict`` and the
    "declared: ..." error text stable without re-sorting at each use.
    """
    for name, declaration in schema.items():
        if not isinstance(declaration, Var):
            raise LanguageError(
                f"schema entry {name!r} must be a var(...), got {type(declaration).__name__}"
            )
        # `env.<name>` has to be able to read it back — see `_require_variable_name`.
        _require_variable_name(name, "schema entry")
    resolved = dict(schema)
    for name, type_ in _WELL_KNOWN.items():
        resolved.setdefault(name, Var(type=type_, default=None))
    return {name: resolved[name] for name in sorted(resolved)}


def _resolve_envs(
    schema: Mapping[str, Var],
    envs: Mapping[str, Mapping[str, Any]],
    view: type[EnvSchema],
) -> dict[str, EnvSchema]:
    """Validate each environment against ``schema``, keyed in sorted name order.

    ``view`` is the class each environment is built as — the user's
    :class:`EnvSchema` subclass, or :class:`EnvView` for the ``var()`` form.
    """
    if not envs:
        raise LanguageError("Config() requires at least one environment in envs=")
    declared = tuple(schema)  # already sorted by `_resolve_schema`
    resolved: dict[str, EnvSchema] = {}
    for name in sorted(envs):
        # Environment names become stack names; checked here so a bad one is
        # reported where it was written rather than as an invalid stack.
        require_identifier(name, "environment")
        resolved[name] = view(name, _resolve_values(schema, name, envs[name]), declared)
    return resolved


def _resolve_values(
    schema: Mapping[str, Var], env_name: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    """One environment's values, defaults filled in and every entry type-checked."""
    if not isinstance(values, Mapping):
        raise LanguageError(
            f"environment {env_name!r} must be a mapping of variable to value, "
            f"got {type(values).__name__}"
        )
    for key in values:
        if key not in schema:
            raise LanguageError(
                f"environment {env_name!r}: unknown variable {key!r} — "
                f"declared: {', '.join(schema)}"
            )
    return {
        key: _resolve_value(declaration, env_name, key, values)
        for key, declaration in schema.items()
    }


def _resolve_value(declaration: Var, env_name: str, key: str, values: Mapping[str, Any]) -> Any:
    """One variable's value: what the environment supplied, or its default."""
    if key not in values:
        if declaration.required:
            raise LanguageError(f"environment {env_name!r} is missing required variable {key!r}")
        return declaration.default

    value = values[key]
    # `None` is legal only where the declaration made the variable nullable by
    # defaulting to it; otherwise it would pass the type check and surface as an
    # empty field on a resource.
    nullable = not declaration.required and declaration.default is None
    if value is None and nullable:
        return None
    if not _type_matches(value, declaration.type):
        raise LanguageError(
            f"environment {env_name!r}: variable {key!r} expects "
            f"{declaration.type.__name__}, got {type(value).__name__} {value!r}"
        )
    return value
