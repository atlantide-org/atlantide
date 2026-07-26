"""Atlas-lang subset validation.

Parses source with the stdlib ``ast`` (every config file is valid Python) and
rejects any construct outside the allowed subset before evaluation, enforcing
determinism by construction.
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
    "ClassDef": "define resource types in a provider, not in config (data + control flow only).",
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
    "super": "class machinery is excluded; config has no classes.",
    "object": "class machinery is excluded; config has no classes.",
    "memoryview": "low-level buffers are excluded for determinism.",
    "breakpoint": "debugger hooks are excluded.",
}

_IMPORT_HINT = (
    "config must be a pure function of its inputs; move helpers into a provider "
    "or use Atlas builtins (`uuid5`, `sha256_hex`, `to_json`, `merge`, `slugify`)."
)

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
