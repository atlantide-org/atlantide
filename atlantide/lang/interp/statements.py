"""Statement handlers: one ``_st_<NodeType>`` method per permitted statement.

Dispatched by name from :meth:`_Evaluator._dispatch`: a statement type without a
handler here cannot execute, even if the validator accepts it.
"""

from __future__ import annotations

import ast
import importlib
from types import ModuleType
from typing import Any

from atlantide.core.config import EnvSchema, check_field_default
from atlantide.core.errors import LanguageError
from atlantide.lang.interp.binding import bind_rejection
from atlantide.lang.interp.evaluator import _Evaluator
from atlantide.lang.interp.scope import Scope, _Break, _Continue, _Return
from atlantide.lang.validate import ENV_SCHEMA_BASE, import_allowed, private_import_message


def _annotation_of(annotation: ast.expr) -> str:
    """An ``EnvSchema`` field's annotation, as the text it was written as.

    Not evaluated, so a `str = 5` earlier in the file cannot change what
    `x: str` declares. Passing text to `EnvSchema.__init_subclass__` gives the
    interpreter and ordinary Python (where ``from __future__ import
    annotations`` yields strings) one parser.
    `validate.schema.check_annotation` has already restricted this to a
    supported name or `X | None`.
    """
    if isinstance(annotation, ast.BinOp):  # `X | None`
        assert isinstance(annotation.left, ast.Name)
        return f"{annotation.left.id} | None"
    assert isinstance(annotation, ast.Name)
    return annotation.id


class StatementsMixin(_Evaluator):
    """The ``_st_*`` handlers. Declares no fields: state lives on `_Evaluator`."""

    def _st_Pass(self, node: ast.Pass, scope: Scope) -> None:
        pass

    def _st_Expr(self, node: ast.Expr, scope: Scope) -> None:
        self._eval(node.value, scope)

    def _st_Assign(self, node: ast.Assign, scope: Scope) -> None:
        value = self._eval(node.value, scope)
        for target in node.targets:
            self._bind(target, value, scope)

    def _st_AnnAssign(self, node: ast.AnnAssign, scope: Scope) -> None:
        if node.value is not None:
            self._bind(node.target, self._eval(node.value, scope), scope)

    def _st_AugAssign(self, node: ast.AugAssign, scope: Scope) -> None:
        if isinstance(node.target, ast.Subscript):
            # As in Python, `d[k()] += 1` evaluates the container and key once.
            container, key = self._subscript(node.target, scope)
            current = container[key]
            rhs = self._eval(node.value, scope)
            container[key] = self._apply_binop(type(node.op), current, rhs)
            return
        current = self._eval_load_target(node.target, scope)
        rhs = self._eval(node.value, scope)
        self._bind(node.target, self._apply_binop(type(node.op), current, rhs), scope)

    def _st_If(self, node: ast.If, scope: Scope) -> None:
        branch = node.body if self._eval(node.test, scope) else node.orelse
        for stmt in branch:
            self._exec(stmt, scope)

    def _st_For(self, node: ast.For, scope: Scope) -> None:
        iterable = self._eval(node.iter, scope)
        for item in self._iter(iterable):
            self._tick()
            self._bind(node.target, item, scope)
            try:
                for stmt in node.body:
                    self._exec(stmt, scope)
            except _Break:
                break
            except _Continue:
                continue
        else:
            for stmt in node.orelse:
                self._exec(stmt, scope)

    def _st_With(self, node: ast.With, scope: Scope) -> None:
        # As in Python: managers enter left to right and exit in reverse with
        # the active exception; a failing __enter__ unwinds those already
        # entered, and a truthy __exit__ suppresses the error. The interpreter
        # calls the dunders itself, so config never needs dunder access.
        managers: list[Any] = []
        try:
            for item in node.items:
                manager = self._eval(item.context_expr, scope)
                entered = type(manager).__enter__(manager)
                managers.append(manager)  # entered: unwound even if the bind fails
                if item.optional_vars is not None:
                    self._bind(item.optional_vars, entered, scope)
            for stmt in node.body:
                self._exec(stmt, scope)
        except (_Break, _Continue, _Return):
            # Control flow is a non-exceptional exit: __exit__ sees no error.
            pending = self._exit_managers(managers, None)
            if pending is not None:
                raise pending from None
            raise
        except BaseException as exc:
            pending = self._exit_managers(managers, exc)
            if pending is not None:
                # `pending` is either `exc` itself or what an __exit__ raised,
                # with its context already set during unwinding.
                raise pending from pending.__cause__
        else:
            pending = self._exit_managers(managers, None)
            if pending is not None:
                raise pending

    @staticmethod
    def _exit_managers(managers: list[Any], exc: BaseException | None) -> BaseException | None:
        """Exit ``managers`` in reverse; return the exception still active after.

        ``exc`` is cleared when some ``__exit__`` returns truthy (suppression),
        and replaced when an ``__exit__`` itself raises. Every entered manager
        is exited either way.
        """
        for manager in reversed(managers):
            try:
                if exc is None:
                    type(manager).__exit__(manager, None, None, None)
                elif type(manager).__exit__(manager, type(exc), exc, exc.__traceback__):
                    exc = None
            except BaseException as raised:
                exc = raised
        return exc

    def _st_Break(self, node: ast.Break, scope: Scope) -> None:
        raise _Break(node)

    def _st_Continue(self, node: ast.Continue, scope: Scope) -> None:
        raise _Continue(node)

    def _st_Return(self, node: ast.Return, scope: Scope) -> None:
        raise _Return(node, self._eval(node.value, scope) if node.value is not None else None)

    def _st_FunctionDef(self, node: ast.FunctionDef, scope: Scope) -> None:
        scope.assign(node.name, self._make_closure(node.args, node.body, scope, node.name))

    def _st_ClassDef(self, node: ast.ClassDef, scope: Scope) -> None:
        """Build the one class config may declare: an ``EnvSchema`` of fields.

        `validate` has already checked the shape; the two guards here are the
        ones a syntactic pass cannot make.
        """
        base = self._eval(node.bases[0], scope)
        # Guard 1: `validate` matches the spelling `EnvSchema`, which a rebinding
        # such as `EnvSchema = S3Bucket` also passes. The base must be the real
        # `EnvSchema`, the only base `type()` below is given.
        if base is not EnvSchema:
            raise LanguageError(
                f"class {node.name!r} must inherit the real {ENV_SCHEMA_BASE}, not a rebound name",
                line=node.lineno,
            )
        annotations, defaults = self._schema_fields(node, scope)
        # Guard 2: `type()` calls `__set_name__` on each direct namespace value,
        # and a default is an arbitrary config value (a Ref, a resource, a
        # closure). Nesting defaults in the `__atlas_defaults__` dict keeps their
        # code from running while the class is built.
        namespace: dict[str, Any] = {
            "__module__": "<config>",
            "__qualname__": node.name,
            "__slots__": (),
            "__annotations__": annotations,
            "__atlas_defaults__": defaults,
        }
        scope.assign(node.name, type(node.name, (EnvSchema,), namespace))

    def _schema_fields(
        self, node: ast.ClassDef, scope: Scope
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """A schema body's field annotations (as written) and evaluated defaults."""
        annotations: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue  # a docstring or `pass`; validate rejected anything else
            self._tick()
            assert isinstance(stmt.target, ast.Name)
            annotations[stmt.target.id] = _annotation_of(stmt.annotation)
            if stmt.value is not None:
                default = self._eval(stmt.value, scope)
                # `__init_subclass__` re-checks every default without a source
                # position; checking here puts the caret on the offending field.
                try:
                    check_field_default(
                        node.name, stmt.target.id, annotations[stmt.target.id], default
                    )
                except LanguageError as exc:
                    raise LanguageError(
                        str(exc), line=stmt.lineno, col=stmt.value.col_offset + 1
                    ) from exc
                defaults[stmt.target.id] = default
        return annotations, defaults

    def _st_Import(self, node: ast.Import, scope: Scope) -> None:
        # Binding a module would expose its attribute graph, including the stdlib
        # modules it imports, to config: a sandbox escape. Only
        # `from ... import <name>` of a non-module object is allowed.
        name = node.names[0].name
        raise LanguageError(
            f"`import {name}` binds a module; use "
            f"`from {name} import <name>` for a specific public symbol",
            line=node.lineno,
        )

    def _st_ImportFrom(self, node: ast.ImportFrom, scope: Scope) -> None:
        assert node.module is not None
        # Re-checked after `validate`: `import_module` executes the target and
        # `getattr` yields a live object, so the allow-list must also hold on the
        # path that binds the name.
        if not import_allowed(node.module, self.surface):
            raise LanguageError(
                f"import from {node.module!r} is not allowed in Atlas-lang",
                line=node.lineno,
            )
        module = importlib.import_module(node.module)
        for alias in node.names:
            self._bind_import(module, alias, node, scope)

    def _bind_import(
        self, module: ModuleType, alias: ast.alias, node: ast.ImportFrom, scope: Scope
    ) -> None:
        """Bind one ``name [as other]`` of an allowed ``from ... import``."""
        assert node.module is not None
        # Leading-underscore helpers (`_read_content`, `_git`) perform IO and are
        # not config API.
        if alias.name.startswith("_"):
            raise LanguageError(private_import_message(alias.name, node.module), line=node.lineno)
        try:
            obj = getattr(module, alias.name)
        except AttributeError:
            raise LanguageError(
                f"cannot import {alias.name!r} from {node.module!r}", line=node.lineno
            ) from None
        reason = bind_rejection(obj, alias.name, node.module, self.surface)
        if reason is not None:
            raise LanguageError(reason, line=node.lineno)
        scope.assign(alias.asname or alias.name, obj)
