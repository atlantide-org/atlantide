"""The validating AST pass and its entry point, :func:`validate_source`."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import override

from returns.result import Failure, Result, Success

from atlantide.core.config import RESERVED_FIELD_NAMES
from atlantide.core.errors import LanguageError
from atlantide.lang.validate.imports import (
    ALLOWED_IMPORTS_DESC,
    DEFAULT_SURFACE,
    FORBIDDEN_CORE_NAMES,
    LanguageSurface,
    engine_import_message,
    import_allowed,
    is_atlantide_module,
    private_import_message,
)
from atlantide.lang.validate.rules import (
    ALLOWED_NODES,
    FORBIDDEN_NAMES,
    IMPORT_HINT,
    NAME_HINTS,
    NODE_HINTS,
    TYPE_PARAMS_HINT,
    attribute_rejection,
    err,
)
from atlantide.lang.validate.schema import (
    ENV_SCHEMA_BASE,
    check_annotation,
    inherits_env_schema,
    is_docstring,
    rejected_in_schema,
)


def _rejected(kind: str, name: str, node: ast.AST, hints: Mapping[str, str]) -> LanguageError:
    """The rejection for ``name``, with its hint appended when one exists."""
    message = f"{kind} {name!r} is not allowed in Atlas-lang"
    hint = hints.get(name)
    return err(f"{message} — {hint}" if hint else message, node)


class _Validator(ast.NodeVisitor):
    """Raises :class:`LanguageError` on the first out-of-subset construct."""

    def __init__(self, surface: LanguageSurface = DEFAULT_SURFACE) -> None:
        self.surface = surface
        #: `id()` of every module-level ClassDef, to tell a schema declared
        #: inside a function, loop or `if` from one at the top level.
        self._toplevel: frozenset[int] = frozenset()
        #: Whether the current statement is in a function body, and in a loop body
        #: of that function or of the module. A `def` resets `_in_loop`: its body
        #: cannot `break` out of an enclosing loop.
        self._in_function = False
        self._in_loop = False

    @override
    def visit_Module(self, node: ast.Module) -> None:
        self._toplevel = frozenset(id(stmt) for stmt in node.body if isinstance(stmt, ast.ClassDef))
        self.generic_visit(node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Allow exactly one class shape: an ``EnvSchema`` of annotated fields.

        ``ClassDef`` is absent from ``rules.ALLOWED_NODES``, so ``generic_visit``
        would reject it. This method checks the shape instead and visits field
        defaults explicitly in :meth:`_check_field`.
        """
        if id(node) not in self._toplevel:
            raise err(
                f"class {node.name!r} must be declared at module level — an "
                f"EnvSchema is a declaration, not a computation",
                node,
            )
        if node.decorator_list:
            raise err(f"a decorator on class {node.name!r} is not allowed", node)
        if node.keywords:
            raise err(
                f"class keyword arguments (metaclass=, ...) are not allowed on {node.name!r}",
                node,
            )
        if node.type_params:  # `class X[T]:`
            raise err(
                f"type parameters on class {node.name!r} are not allowed — {TYPE_PARAMS_HINT}", node
            )
        if not inherits_env_schema(node):
            raise err(
                f"class {node.name!r} must inherit exactly {ENV_SCHEMA_BASE} — config "
                f"declares no other classes; define resource types in a provider",
                node,
            )
        self._check_schema_body(node)

    def _check_schema_body(self, node: ast.ClassDef) -> None:
        seen: set[str] = set()
        for index, stmt in enumerate(node.body):
            if isinstance(stmt, ast.Pass) or (index == 0 and is_docstring(stmt)):
                continue
            if not isinstance(stmt, ast.AnnAssign):
                raise rejected_in_schema(stmt)
            self._check_field(stmt, node.name, seen)

    def _check_field(self, stmt: ast.AnnAssign, owner: str, seen: set[str]) -> None:
        if not stmt.simple or not isinstance(stmt.target, ast.Name):
            raise err(f"a field of {owner!r} must be a plain name", stmt)
        field = stmt.target.id
        if field.startswith("_"):
            raise err(
                f"field {field!r} of {owner!r} must not start with '_' — "
                f"it is read back as `env.{field}`",
                stmt,
            )
        if field in RESERVED_FIELD_NAMES:
            raise err(
                f"field {field!r} of {owner!r} collides with an environment's own API "
                f"({', '.join(sorted(RESERVED_FIELD_NAMES))}) — pick another name",
                stmt,
            )
        if field in seen:
            raise err(f"field {field!r} of {owner!r} is declared twice", stmt)
        seen.add(field)
        check_annotation(stmt.annotation, owner, field)
        if stmt.value is not None:
            # A default is an ordinary expression and must meet every other rule.
            self.visit(stmt.value)

    @override
    def generic_visit(self, node: ast.AST) -> None:
        self._check_allowed(node)
        super().generic_visit(node)

    @staticmethod
    def _check_allowed(node: ast.AST) -> None:
        name = type(node).__name__
        if name not in ALLOWED_NODES:
            raise _rejected("construct", name, node, NODE_HINTS)

    # -- statements valid only inside a loop or function -------------------
    #
    # `ast.parse` accepts these anywhere; CPython rejects them only at compile
    # time, which config never reaches. They are reported with CPython's
    # syntax-error wording.

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.decorator_list:
            # The interpreter would ignore it: `@cache def f` must not silently
            # mean an undecorated `f`.
            raise err(f"a decorator on function {node.name!r} is not allowed", node)
        if node.type_params:  # `def f[T]():`
            raise err(
                f"type parameters on function {node.name!r} are not allowed — {TYPE_PARAMS_HINT}",
                node,
            )
        self._check_params(node.args, f"function {node.name!r}", node)
        outer = self._in_function, self._in_loop
        self._in_function, self._in_loop = True, False
        self.generic_visit(node)
        self._in_function, self._in_loop = outer

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._check_params(node.args, "a lambda", node)
        self.generic_visit(node)

    @staticmethod
    def _check_params(args: ast.arguments, owner: str, node: ast.AST) -> None:
        """Only plain positional parameters, with or without defaults, are supported."""
        if args.vararg is not None:
            raise err(f"`*{args.vararg.arg}` parameters are not allowed on {owner}", node)
        if args.kwarg is not None:
            raise err(f"`**{args.kwarg.arg}` parameters are not allowed on {owner}", node)
        if args.kwonlyargs:
            raise err(f"keyword-only parameters (after `*`) are not allowed on {owner}", node)
        if args.posonlyargs:
            raise err(f"positional-only parameters (before `/`) are not allowed on {owner}", node)

    @override
    def visit_For(self, node: ast.For) -> None:
        self._check_allowed(node)
        self.visit(node.target)
        self.visit(node.iter)
        outer = self._in_loop
        self._in_loop = True
        for stmt in node.body:
            self.visit(stmt)
        # `else:` runs after the loop, not inside it.
        self._in_loop = outer
        for stmt in node.orelse:
            self.visit(stmt)

    @override
    def visit_Break(self, node: ast.Break) -> None:
        if not self._in_loop:
            raise err("syntax error: 'break' outside loop", node)
        self.generic_visit(node)

    @override
    def visit_Continue(self, node: ast.Continue) -> None:
        if not self._in_loop:
            raise err("syntax error: 'continue' not properly in loop", node)
        self.generic_visit(node)

    @override
    def visit_Return(self, node: ast.Return) -> None:
        if not self._in_function:
            raise err("syntax error: 'return' outside function", node)
        self.generic_visit(node)

    @override
    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            raise err(f"dunder name {node.id!r} is not allowed", node)
        if node.id in FORBIDDEN_NAMES:
            raise _rejected("name", node.id, node, NAME_HINTS)
        self.generic_visit(node)

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (reason := attribute_rejection(node.attr)) is not None:
            raise err(reason, node)
        self.generic_visit(node)

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not import_allowed(alias.name, self.surface):
                raise err(
                    f"import of {alias.name!r} is not allowed "
                    f"(only {ALLOWED_IMPORTS_DESC}) — {IMPORT_HINT}",
                    node,
                )
        self.generic_visit(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or not import_allowed(node.module, self.surface):
            target = node.module or "."
            raise err(
                f"import from {target!r} is not allowed "
                f"(only {ALLOWED_IMPORTS_DESC}) — {IMPORT_HINT}",
                node,
            )
        module = node.module  # non-None: `import_allowed` rejects a missing module
        assert module is not None
        for alias in node.names:
            if alias.name.startswith("_"):
                raise err(private_import_message(alias.name, module), node)
            if is_atlantide_module(module) and alias.name in FORBIDDEN_CORE_NAMES:
                raise err(engine_import_message(alias.name, module), node)
        self.generic_visit(node)


#: Reported when source nests deeper than the parser or validator can recurse.
_TOO_DEEP = "expression nested too deeply"


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
    except (RecursionError, MemoryError):
        # CPython's parser gives up on deep nesting with one of these.
        return Failure(LanguageError(_TOO_DEEP))
    try:
        _Validator(surface).visit(module)
    except LanguageError as exc:
        return Failure(exc)
    except RecursionError:
        # Parsed, but deeper than the recursive visitor (or the interpreter) can walk.
        return Failure(LanguageError(_TOO_DEEP))
    return Success(module)
