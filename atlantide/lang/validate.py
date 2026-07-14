"""Atlas-lang subset validation.

Parses source with the stdlib ``ast`` (every config file is valid Python) and
rejects any construct outside the allowed subset before evaluation, enforcing
determinism by construction.
"""

from __future__ import annotations

import ast

from returns.result import Failure, Result, Success

from atlantide.core.errors import LanguageError

# Node type names permitted anywhere in a config module.
_ALLOWED_NODES: frozenset[str] = frozenset(
    {
        # module + statements
        "Module", "FunctionDef", "Return", "Assign", "AnnAssign", "AugAssign",
        "Expr", "If", "For", "Pass", "Break", "Continue", "Import", "ImportFrom",
        "alias", "With", "withitem",
        # expressions
        "Constant", "Name", "FormattedValue", "JoinedStr", "BinOp", "UnaryOp",
        "BoolOp", "Compare", "IfExp", "Call", "keyword", "Attribute",
        "Subscript", "Slice", "List", "Tuple", "Set", "Dict", "ListComp",
        "SetComp", "DictComp", "GeneratorExp", "comprehension", "Lambda",
        "Starred", "arguments", "arg",
        # contexts
        "Load", "Store",
        # operators
        "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow", "LShift",
        "RShift", "BitOr", "BitAnd", "BitXor", "And", "Or", "Not", "USub",
        "UAdd", "Invert", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "In",
        "NotIn", "Is", "IsNot",
    }
)

# Builtins that are dangerous even if never injected — rejected for clear errors.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "globals", "locals",
        "vars", "getattr", "setattr", "delattr", "hasattr", "breakpoint",
        "input", "memoryview", "type", "super", "object",
    }
)

_IMPORT_PREFIX = "atlantide"

# Internal implementation packages. Config imports only the public surface
# (`atlantide.core`, `atlantide.policy`, `atlantide.providers.*`); the packages
# below expose the interpreter/IR internals — and, transitively, the stdlib
# modules they import (`importlib`, `operator`, `base64`, ...) — so importing
# from them is a sandbox escape. See `_import_allowed`.
_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "atlantide.lang",
    "atlantide.ir",
    "atlantide.engine",
)

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


def _err(message: str, node: ast.AST) -> LanguageError:
    line = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    return LanguageError(message, line=line, col=col)


def _import_allowed(module: str | None) -> bool:
    if not module:
        return False
    if module != _IMPORT_PREFIX and not module.startswith(_IMPORT_PREFIX + "."):
        return False
    return not any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in _FORBIDDEN_IMPORT_PREFIXES
    )


class _Validator(ast.NodeVisitor):
    """Raises :class:`LanguageError` on the first out-of-subset construct."""

    def generic_visit(self, node: ast.AST) -> None:
        name = type(node).__name__
        if name not in _ALLOWED_NODES:
            message = f"construct {name!r} is not allowed in Atlas-lang"
            hint = _NODE_HINTS.get(name)
            if hint:
                message += f" — {hint}"
            raise _err(message, node)
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            raise _err(f"dunder name {node.id!r} is not allowed", node)
        if node.id in _FORBIDDEN_NAMES:
            message = f"name {node.id!r} is not allowed in Atlas-lang"
            hint = _NAME_HINTS.get(node.id)
            if hint:
                message += f" — {hint}"
            raise _err(message, node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            raise _err(f"dunder attribute {node.attr!r} is not allowed", node)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _import_allowed(alias.name):
                raise _err(
                    f"import of {alias.name!r} is not allowed "
                    f"(only 'atlantide[.*]' modules) — {_IMPORT_HINT}",
                    node,
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or not _import_allowed(node.module):
            target = node.module or "."
            raise _err(
                f"import from {target!r} is not allowed "
                f"(only 'atlantide[.*]' modules) — {_IMPORT_HINT}",
                node,
            )
        self.generic_visit(node)


def validate_source(source: str, filename: str = "<config>") -> Result[ast.Module, LanguageError]:
    """Parse and subset-check config source. Success carries the parsed module."""
    try:
        module = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        return Failure(LanguageError(f"syntax error: {exc.msg}", line=exc.lineno, col=exc.offset))
    try:
        _Validator().visit(module)
    except LanguageError as exc:
        return Failure(exc)
    return Success(module)
