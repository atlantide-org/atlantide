"""The ``EnvSchema`` carve-out: the one class shape config may declare."""

from __future__ import annotations

import ast

from atlantide.core.config import SUPPORTED_FIELD_TYPE_NAMES
from atlantide.core.errors import LanguageError
from atlantide.lang.validate.rules import err

#: The one base a config-declared class may have (see `inherits_env_schema`).
ENV_SCHEMA_BASE = "EnvSchema"


def is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


#: Alternatives for common non-field statements in a schema body, keyed by AST
#: node type name like `NODE_HINTS`.
_SCHEMA_BODY_HINTS: dict[str, str] = {
    "Assign": "write 'x: str = 1' with a type",
    "FunctionDef": "move behaviour into a provider or a component",
    "ClassDef": "a schema is flat; declare a second one at module level",
}


def inherits_env_schema(node: ast.ClassDef) -> bool:
    """Whether the class names ``EnvSchema`` as its one base.

    Checks the *spelling* only: ``EnvSchema = S3Bucket`` earlier in the file passes,
    so ``interp.statements.StatementsMixin._st_ClassDef`` re-checks the object the
    base resolves to.
    """
    return (
        len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and node.bases[0].id == ENV_SCHEMA_BASE
    )


def rejected_in_schema(stmt: ast.stmt) -> LanguageError:
    """The rejection for a schema-body statement that is not an annotated field."""
    kind = type(stmt).__name__
    message = (
        f"{kind!r} is not allowed in an {ENV_SCHEMA_BASE} — "
        f"it declares annotated fields only (data, no behaviour)"
    )
    hint = _SCHEMA_BODY_HINTS.get(kind)
    return err(f"{message}; {hint}" if hint else message, stmt)


def check_annotation(annotation: ast.expr, owner: str, field: str) -> None:
    """Allow one of the six supported types, or ``X | None``.

    Annotations are matched by name against ``SUPPORTED_FIELD_TYPE_NAMES`` (the
    types ``var()`` accepts) and never evaluated, here or by the interpreter:
    evaluation would let ``str = 5`` earlier in the file change what ``x: str``
    means.
    """
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        right = annotation.right
        if not (isinstance(right, ast.Constant) and right.value is None):
            raise err(f"field {field!r} of {owner!r}: only `X | None` may be combined", annotation)
        check_annotation(annotation.left, owner, field)
        return
    if isinstance(annotation, ast.Subscript):
        raise err(
            f"field {field!r} of {owner!r}: parameterised generics such as list[str] "
            f"are not supported — use `list`",
            annotation,
        )
    supported = SUPPORTED_FIELD_TYPE_NAMES
    if not isinstance(annotation, ast.Name) or annotation.id not in supported:
        raise err(
            f"field {field!r} of {owner!r} must be one of {', '.join(sorted(supported))}",
            annotation,
        )
