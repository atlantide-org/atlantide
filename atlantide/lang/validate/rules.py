"""What config may write, node by node: the construct, name and attribute rules.

A rejection carries a hint, where one exists, saying why the construct is excluded
and what to write instead. The import rules live in ``imports``.
"""

from __future__ import annotations

import ast
import types

from atlantide.core.errors import LanguageError

# Node type names permitted anywhere in a config module.
ALLOWED_NODES: frozenset[str] = frozenset(
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
FORBIDDEN_NAMES: frozenset[str] = frozenset(
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

# `str.format`'s field syntax (`"{0.__class__.__init__.__globals__[x]}"`) walks
# attributes on a live object, reaching the interpreter's own globals. The
# template is an `ast.Constant`, so `visit_Name`/`visit_Attribute` never see the
# dunder; the method itself is rejected instead.
_FORBIDDEN_ATTRS: frozenset[str] = frozenset({"format", "format_map"})

_FORMAT_HINT = "use an f-string, or `interpolate(template, *args)` for apply-time values."

#: Pydantic model API, rejected by name: `Widget.parse_file(path)` reads the disk
#: and `model_construct` skips validation. Only names no resource would declare as
#: a field are listed; plausible field names (`json`, `copy`, `schema`, ...) are
#: decided per object by `interp.expressions._attribute_allowed`.
_MODEL_API_ATTRS: frozenset[str] = frozenset(
    {
        "construct",
        "from_orm",
        "model_computed_fields",
        "model_config",
        "model_construct",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "model_extra",
        "model_fields",
        "model_fields_set",
        "model_json_schema",
        "model_parametrized_name",
        "model_post_init",
        "model_rebuild",
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "parse_file",
        "parse_obj",
        "parse_raw",
        "schema_json",
        "update_forward_refs",
    }
)
_MODEL_API_HINT = "a resource exposes its declared fields; pydantic's model API is not config."

#: The public attributes of generators, coroutines, frames, tracebacks, code and
#: cell objects: `gi_frame` -> `f_back`/`f_globals`/`f_builtins` walks out of the
#: sandbox without a single underscore. Taken from the types themselves so a
#: newer interpreter's additions are covered; each prefix is specific to its type.
#: The interpreter also refuses these objects by type (`_attribute_allowed`).
_INTERNAL_ATTRS: frozenset[str] = frozenset(
    name
    for kind, prefix in (
        (types.GeneratorType, "gi_"),
        (types.CoroutineType, "cr_"),
        (types.AsyncGeneratorType, "ag_"),
        (types.FrameType, "f_"),
        (types.TracebackType, "tb_"),
        (types.CodeType, "co_"),
        (types.CellType, "cell_"),
    )
    for name in dir(kind)
    if name.startswith(prefix)
) | {
    # Listed as well, in case an interpreter version lacks them.
    "gi_frame",
    "gi_code",
    "gi_yieldfrom",
    "cr_frame",
    "cr_code",
    "cr_await",
    "ag_frame",
    "ag_code",
    "ag_await",
    "f_globals",
    "f_locals",
    "f_builtins",
    "f_back",
    "f_code",
    "tb_frame",
    "tb_next",
    "cell_contents",
}
_INTERNAL_HINT = "interpreter internals (generators, frames, code objects) are not config."

_ATTR_HINTS: dict[str, str] = {
    **dict.fromkeys(_FORBIDDEN_ATTRS, _FORMAT_HINT),
    **dict.fromkeys(_MODEL_API_ATTRS, _MODEL_API_HINT),
    **dict.fromkeys(_INTERNAL_ATTRS, _INTERNAL_HINT),
}

# Hints appended to a rejection (why excluded, what to use instead), keyed by
# AST node type name.
NODE_HINTS: dict[str, str] = {
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
    "TypeAlias": "config has no static types to alias; assign the value to a name instead.",
}

#: Why ``def f[T]()`` / ``class C[T]`` are refused: generics are static-typing syntax.
TYPE_PARAMS_HINT = "config has no static types; drop the `[...]` type parameters."

NAME_HINTS: dict[str, str] = {
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

IMPORT_HINT = (
    "config must be a pure function of its inputs; move helpers into a provider "
    "or use Atlas builtins (`uuid5`, `sha256_hex`, `to_json`, `merge`, `slugify`)."
)


def attribute_rejection(name: str) -> str | None:
    """Why config may not read attribute ``name`` on any object, or ``None``.

    Shared with the interpreter, which re-applies it to the attribute actually
    read. A leading underscore marks private state (``atlantide._inputs``, a
    resource's ``_stack``) as well as dunders.
    """
    if name.startswith("__"):
        return f"dunder attribute {name!r} is not allowed"
    if name.startswith("_"):
        return f"private attribute {name!r} is not allowed — config reads public API only"
    if name in _ATTR_HINTS:
        return f"attribute {name!r} is not allowed in Atlas-lang — {_ATTR_HINTS[name]}"
    return None


def err(message: str, node: ast.AST) -> LanguageError:
    line = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    # ast's col_offset is 0-based; LanguageError (and the CLI caret rendering)
    # use 1-based columns, matching SyntaxError's `offset`.
    return LanguageError(message, line=line, col=col + 1 if isinstance(col, int) else None)
