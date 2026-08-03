"""Atlas-lang subset validation.

Parses source with the stdlib ``ast`` (every config file is valid Python) and
rejects any construct outside the allowed subset before evaluation, enforcing
determinism by construction.

The subset is expressed two ways. :data:`_ALLOWED_NODES` is the allow-list of
node types permitted anywhere; ``ClassDef`` is deliberately absent from it and
handled by :meth:`_Validator.visit_ClassDef` instead, because a class is
permitted only in one exact shape — a module-level ``EnvSchema`` whose body is
annotated fields. That carve-out is what makes an environment's variables
attributes a type checker can see; a schema declares data, runs no code and never
reaches the IR. The two checks a syntactic pass cannot make — that the base
really is ``EnvSchema``, and that a default never lands in the class namespace —
are made by ``Interpreter._st_ClassDef``.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.errors import LanguageError

# Node type names permitted anywhere in a config module.
_ALLOWED_NODES: frozenset[str] = frozenset(
    {
        # module + statements
        "Module",
        "FunctionDef",
        "Return",
        "Assign",
        "AnnAssign",
        "AugAssign",
        "Expr",
        "If",
        "For",
        "Pass",
        "Break",
        "Continue",
        "Import",
        "ImportFrom",
        "alias",
        "With",
        "withitem",
        # expressions
        "Constant",
        "Name",
        "FormattedValue",
        "JoinedStr",
        "BinOp",
        "UnaryOp",
        "BoolOp",
        "Compare",
        "IfExp",
        "Call",
        "keyword",
        "Attribute",
        "Subscript",
        "Slice",
        "List",
        "Tuple",
        "Set",
        "Dict",
        "ListComp",
        "SetComp",
        "DictComp",
        "GeneratorExp",
        "comprehension",
        "Lambda",
        "Starred",
        "arguments",
        "arg",
        # contexts
        "Load",
        "Store",
        # operators
        "Add",
        "Sub",
        "Mult",
        "Div",
        "FloorDiv",
        "Mod",
        "Pow",
        "LShift",
        "RShift",
        "BitOr",
        "BitAnd",
        "BitXor",
        "And",
        "Or",
        "Not",
        "USub",
        "UAdd",
        "Invert",
        "Eq",
        "NotEq",
        "Lt",
        "LtE",
        "Gt",
        "GtE",
        "In",
        "NotIn",
        "Is",
        "IsNot",
    }
)

# Builtins rejected by name, even when not injected, so config gets a clear error.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "breakpoint",
        "input",
        "memoryview",
        "type",
        "super",
        "object",
    }
)

_IMPORT_PREFIX = "atlantide"

# The public config surface, as an allow-list. Modules outside these packages
# (`atlantide.secrets`, `.state`, `.cli`, `.reconcile`, ...) import stdlib IO —
# `os`, `subprocess`, `pathlib` — and the interpreter would bind those into
# config scope, which is a sandbox escape. See `import_allowed`.
_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = (
    "atlantide.core",
    "atlantide.policy",
    "atlantide.providers",
    "atlantide.components",
)

# Modules inside the allowed packages that still reach IO or nondeterminism: the
# component fetcher shells out to git, and `provider` / `handlers` modules hold
# the boto3, filesystem, and `uuid`/`token_hex` calls. Config declares resources
# rather than driving them.
_FORBIDDEN_IMPORT_MODULES: frozenset[str] = frozenset(
    {
        "atlantide.components.fetch",
        "atlantide.components.lock",
        "atlantide.components.source",
    }
)

# Dotted segments marking an internal module anywhere under the allowed prefixes
# (`atlantide.providers.aws.provider`, `...aws.handlers.s3`).
_FORBIDDEN_IMPORT_SEGMENTS: frozenset[str] = frozenset({"provider", "handlers"})

# `str.format`'s field syntax (`"{0.__class__.__init__.__globals__[x]}"`) walks
# attributes on a live object, reaching the interpreter's own globals. The
# template is an `ast.Constant`, so `visit_Name`/`visit_Attribute` never see the
# dunder — hence rejecting the method itself.
_FORBIDDEN_ATTRS: frozenset[str] = frozenset({"format", "format_map"})

_FORMAT_HINT = "use an f-string, or `interpolate(template, *args)` for apply-time values."
_ATTR_HINTS: dict[str, str] = dict.fromkeys(_FORBIDDEN_ATTRS, _FORMAT_HINT)

# Hints appended to a rejection (why excluded, what to use instead), keyed by
# AST node type name.
_NODE_HINTS: dict[str, str] = {
    "While": "Atlas-lang has no `while` (halting must be provable); use a bounded `for`.",
    "Try": "no exceptions in config; guard with `if` instead.",
    "Raise": "no exceptions in config; guard with `if` instead.",
    "AsyncFunctionDef": "config is synchronous and pure; no `async`.",
    "Await": "config is synchronous and pure; no `await`.",
    "Yield": "generators are not allowed; build lists with comprehensions.",
    "YieldFrom": "generators are not allowed; build lists with comprehensions.",
    "Global": "no mutable module state; pass values as function arguments.",
    "Nonlocal": "no mutable closure state; pass values as function arguments.",
    "NamedExpr": "walrus `:=` is not allowed; use a separate assignment.",
    "Delete": "`del` is not allowed; bound values are immutable.",
}

# Hints for forbidden builtin names.
_NAME_HINTS: dict[str, str] = {
    "eval": "dynamic code execution is excluded for determinism.",
    "exec": "dynamic code execution is excluded for determinism.",
    "compile": "dynamic code execution is excluded for determinism.",
    "__import__": "use a top-level `import atlantide...` statement instead.",
    "open": "file/network IO does not exist; config is a pure function of its inputs.",
    "input": "no interactive/environment input; use `atlantide.input(name)`.",
    "getattr": "dynamic attribute access is excluded for determinism.",
    "setattr": "dynamic attribute access is excluded for determinism.",
    "delattr": "dynamic attribute access is excluded for determinism.",
    "hasattr": "dynamic attribute access is excluded for determinism.",
    "vars": "dynamic introspection is excluded for determinism.",
    "globals": "dynamic introspection is excluded for determinism.",
    "locals": "dynamic introspection is excluded for determinism.",
    "type": "runtime type construction is excluded; define resource types in a provider.",
    "super": "class machinery is excluded; the only class config declares is an EnvSchema.",
    "object": "class machinery is excluded; the only class config declares is an EnvSchema.",
    "memoryview": "low-level buffers are excluded for determinism.",
    "breakpoint": "debugger hooks are excluded.",
}

_IMPORT_HINT = (
    "config must be a pure function of its inputs; move helpers into a provider "
    "or use Atlas builtins (`uuid5`, `sha256_hex`, `to_json`, `merge`, `slugify`)."
)

#: The one base a config-declared class may have. Checked here by *spelling*
#: only; `Interpreter._st_ClassDef` re-checks the bound object's identity, since
#: `EnvSchema = S3Bucket` earlier in the file would pass this test.
ENV_SCHEMA_BASE = "EnvSchema"

#: Types an ``EnvSchema`` field may be annotated with — the same set `var()`
#: accepts (`atlantide.core.config._SUPPORTED_TYPES`), spelled as source because
#: the validator works on an AST and must not import core.
_FIELD_TYPES: frozenset[str] = frozenset({"str", "int", "float", "bool", "list", "dict"})

#: Field names that would be unreachable as `env.<name>`, mirroring
#: `atlantide.core.config._RESERVED`.
_RESERVED_FIELDS: frozenset[str] = frozenset({"name", "get", "as_dict"})

#: Rendered in import rejections so the message names the surface, not the rule.
_ALLOWED_IMPORTS_DESC = "'atlantide.core', '.policy', '.providers.*', '.components.*'"


def private_import_message(name: str, module: str) -> str:
    """Rejection text for a leading-underscore import, shared with the interpreter."""
    return f"cannot import private name {name!r} from {module!r}; config imports only public API"


def _err(message: str, node: ast.AST) -> LanguageError:
    line = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    # ast's col_offset is 0-based; LanguageError (and the CLI caret rendering)
    # use 1-based columns, matching SyntaxError's `offset`.
    return LanguageError(message, line=line, col=col + 1 if isinstance(col, int) else None)


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


#: What to write instead, for the statements an author is most likely to reach
#: for inside a schema body. Keyed by AST node type name, like `_NODE_HINTS`.
_SCHEMA_BODY_HINTS: dict[str, str] = {
    "Assign": "write 'x: str = 1' with a type",
    "FunctionDef": "move behaviour into a provider or a component",
    "ClassDef": "a schema is flat; declare a second one at module level",
}


def _inherits_env_schema(node: ast.ClassDef) -> bool:
    """Whether the class names ``EnvSchema`` as its one base.

    A check on the *spelling* only — ``EnvSchema = S3Bucket`` earlier in the file
    passes it, which is why ``Interpreter._st_ClassDef`` re-checks the object it
    actually resolves to.
    """
    return (
        len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and node.bases[0].id == ENV_SCHEMA_BASE
    )


def _rejected_in_schema(stmt: ast.stmt) -> LanguageError:
    """The rejection for a schema-body statement that is not an annotated field."""
    kind = type(stmt).__name__
    message = (
        f"{kind!r} is not allowed in an {ENV_SCHEMA_BASE} — "
        f"it declares annotated fields only (data, no behaviour)"
    )
    hint = _SCHEMA_BODY_HINTS.get(kind)
    return _err(f"{message}; {hint}" if hint else message, stmt)


def _check_annotation(annotation: ast.expr, owner: str, field: str) -> None:
    """Allow one of the six supported types, or ``X | None``.

    Annotations are never evaluated, here or by the interpreter: treating them
    as expressions would let ``str = 5`` earlier in the file change what
    ``x: str`` means.
    """
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        right = annotation.right
        if not (isinstance(right, ast.Constant) and right.value is None):
            raise _err(f"field {field!r} of {owner!r}: only `X | None` may be combined", annotation)
        _check_annotation(annotation.left, owner, field)
        return
    if isinstance(annotation, ast.Subscript):
        raise _err(
            f"field {field!r} of {owner!r}: parameterised generics such as list[str] "
            f"are not supported — use `list`",
            annotation,
        )
    if not isinstance(annotation, ast.Name) or annotation.id not in _FIELD_TYPES:
        raise _err(
            f"field {field!r} of {owner!r} must be one of {', '.join(sorted(_FIELD_TYPES))}",
            annotation,
        )


def _rejected(kind: str, name: str, node: ast.AST, hints: Mapping[str, str]) -> LanguageError:
    """The rejection for ``name``, with its hint appended when one exists."""
    message = f"{kind} {name!r} is not allowed in Atlas-lang"
    hint = hints.get(name)
    return _err(f"{message} — {hint}" if hint else message, node)


@dataclass(frozen=True, slots=True)
class LanguageSurface:
    """Which modules config may import.

    The built-in prefixes plus whatever installed provider plugins contribute.
    Passed explicitly rather than kept in a module global: a global would make
    what a config is *allowed* to say depend on what happened to be loaded first,
    so the same file could validate in one command and not in another.

    Third-party modules are held to the same internal-module rules as the
    built-ins — a plugin's ``provider``/``handlers`` submodules hold its network
    and filesystem calls, and binding those into config scope is the escape the
    allow-list exists to prevent.
    """

    extra: frozenset[str] = frozenset()

    def prefixes(self) -> tuple[str, ...]:
        return (*_ALLOWED_IMPORT_PREFIXES, *sorted(self.extra))


#: The surface with no plugins loaded — what the shipped providers alone allow.
DEFAULT_SURFACE = LanguageSurface()


def import_allowed(module: str | None, surface: LanguageSurface = DEFAULT_SURFACE) -> bool:
    if not module:
        return False
    if module == _IMPORT_PREFIX:
        return True
    if not any(
        module == prefix or module.startswith(prefix + ".") for prefix in surface.prefixes()
    ):
        return False
    if module in _FORBIDDEN_IMPORT_MODULES:
        return False
    return not _FORBIDDEN_IMPORT_SEGMENTS.intersection(module.split("."))


class _Validator(ast.NodeVisitor):
    """Raises :class:`LanguageError` on the first out-of-subset construct."""

    def __init__(self, surface: LanguageSurface = DEFAULT_SURFACE) -> None:
        self.surface = surface
        #: `id()` of every module-level ClassDef, to tell a schema declared
        #: inside a function, loop or `if` from one at the top level.
        self._toplevel: frozenset[int] = frozenset()

    @override
    def visit_Module(self, node: ast.Module) -> None:
        self._toplevel = frozenset(id(stmt) for stmt in node.body if isinstance(stmt, ast.ClassDef))
        self.generic_visit(node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Allow exactly one class shape: an ``EnvSchema`` of annotated fields.

        Defining this method intercepts the node before ``generic_visit``, which
        is why ``"ClassDef"`` stays out of :data:`_ALLOWED_NODES`: the node is
        not permitted in general, only in the shape checked here.

        Interception also means children are not visited automatically, and
        ``generic_visit`` cannot fix that (it would re-check ``ClassDef``
        against the allow-list and reject). Default expressions are therefore
        visited explicitly in :meth:`_check_field`.
        """
        if id(node) not in self._toplevel:
            raise _err(
                f"class {node.name!r} must be declared at module level — an "
                f"EnvSchema is a declaration, not a computation",
                node,
            )
        if node.decorator_list:
            raise _err(f"a decorator on class {node.name!r} is not allowed", node)
        if node.keywords:
            raise _err(
                f"class keyword arguments (metaclass=, ...) are not allowed on {node.name!r}",
                node,
            )
        if getattr(node, "type_params", ()):  # `class X[T]:` — 3.12+
            raise _err(f"type parameters on class {node.name!r} are not allowed", node)
        if not _inherits_env_schema(node):
            raise _err(
                f"class {node.name!r} must inherit exactly {ENV_SCHEMA_BASE} — config "
                f"declares no other classes; define resource types in a provider",
                node,
            )
        self._check_schema_body(node)

    def _check_schema_body(self, node: ast.ClassDef) -> None:
        seen: set[str] = set()
        for index, stmt in enumerate(node.body):
            if isinstance(stmt, ast.Pass) or (index == 0 and _is_docstring(stmt)):
                continue
            if not isinstance(stmt, ast.AnnAssign):
                raise _rejected_in_schema(stmt)
            self._check_field(stmt, node.name, seen)

    def _check_field(self, stmt: ast.AnnAssign, owner: str, seen: set[str]) -> None:
        if not stmt.simple or not isinstance(stmt.target, ast.Name):
            raise _err(f"a field of {owner!r} must be a plain name", stmt)
        field = stmt.target.id
        if field.startswith("_"):
            raise _err(
                f"field {field!r} of {owner!r} must not start with '_' — "
                f"it is read back as `env.{field}`",
                stmt,
            )
        if field in _RESERVED_FIELDS:
            raise _err(
                f"field {field!r} of {owner!r} collides with an environment's own API "
                f"({', '.join(sorted(_RESERVED_FIELDS))}) — pick another name",
                stmt,
            )
        if field in seen:
            raise _err(f"field {field!r} of {owner!r} is declared twice", stmt)
        seen.add(field)
        _check_annotation(stmt.annotation, owner, field)
        if stmt.value is not None:
            # A default is an ordinary expression and must meet every other rule.
            self.visit(stmt.value)

    @override
    def generic_visit(self, node: ast.AST) -> None:
        name = type(node).__name__
        if name not in _ALLOWED_NODES:
            raise _rejected("construct", name, node, _NODE_HINTS)
        super().generic_visit(node)

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            raise _err(f"dunder name {node.id!r} is not allowed", node)
        if node.id in _FORBIDDEN_NAMES:
            raise _rejected("name", node.id, node, _NAME_HINTS)
        self.generic_visit(node)

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            raise _err(f"dunder attribute {node.attr!r} is not allowed", node)
        if node.attr in _FORBIDDEN_ATTRS:
            raise _rejected("attribute", node.attr, node, _ATTR_HINTS)
        self.generic_visit(node)

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not import_allowed(alias.name, self.surface):
                raise _err(
                    f"import of {alias.name!r} is not allowed "
                    f"(only {_ALLOWED_IMPORTS_DESC}) — {_IMPORT_HINT}",
                    node,
                )
        self.generic_visit(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or not import_allowed(node.module, self.surface):
            target = node.module or "."
            raise _err(
                f"import from {target!r} is not allowed "
                f"(only {_ALLOWED_IMPORTS_DESC}) — {_IMPORT_HINT}",
                node,
            )
        module = node.module  # non-None: `import_allowed` rejects a missing module
        assert module is not None
        for alias in node.names:
            if alias.name.startswith("_"):
                raise _err(private_import_message(alias.name, module), node)
        self.generic_visit(node)


def validate_source(
    source: str,
    filename: str = "<config>",
    surface: LanguageSurface = DEFAULT_SURFACE,
) -> Result[ast.Module, LanguageError]:
    """Parse and subset-check config source. Success carries the parsed module."""
    try:
        module = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        return Failure(LanguageError(f"syntax error: {exc.msg}", line=exc.lineno, col=exc.offset))
    try:
        _Validator(surface).visit(module)
    except LanguageError as exc:
        return Failure(exc)
    return Success(module)
