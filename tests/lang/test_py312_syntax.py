"""Python 3.12+ typing syntax is refused by the Atlas-lang validator.

Config is parsed with the runtime's ``ast``, so on 3.12+ ``type X = ...``,
``def f[T]()`` and ``class C[T]`` parse. None of them is part of Atlas-lang: each
must fail validation (and therefore evaluation) with a clear, located error.
"""

from __future__ import annotations

import ast
import sys

import pytest

from atlantide.core import LanguageError, is_successful
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.validate.rules import ALLOWED_NODES


def _rejection(source: str) -> LanguageError:
    result = validate_source(source)
    assert not is_successful(result), f"accepted: {source!r}"
    error = result.failure()
    assert isinstance(error, LanguageError)
    return error


TYPE_ALIASES = [
    ("plain", "type X = int\n", 1),
    ("generic", "type Pair[T] = tuple[T, T]\n", 1),
    ("bounded", "type Box[T: int] = list[T]\n", 1),
    ("param_spec", "type Fn[**P] = int\n", 1),
    ("type_var_tuple", "type Ts[*A] = tuple[*A]\n", 1),
    ("in_function", "def f():\n    type X = int\n    return 1\n", 2),
    ("in_if", "if True:\n    type X = int\n", 2),
    ("in_for", "for i in range(2):\n    type X = int\n", 2),
]


@pytest.mark.parametrize(
    ("source", "line"), [(s, n) for _, s, n in TYPE_ALIASES], ids=[i for i, _, _ in TYPE_ALIASES]
)
def test_type_statement_is_rejected(source: str, line: int) -> None:
    error = _rejection(source)
    message = str(error)
    assert "construct 'TypeAlias' is not allowed in Atlas-lang" in message
    assert "assign the value to a name instead" in message
    assert f"(line {line}," in message


FUNCTION_TYPE_PARAMS = [
    ("type_var", "def f[T](x):\n    return x\n", 1),
    ("bound", "def f[T: int](x):\n    return x\n", 1),
    ("constrained", "def f[T: (int, str)](x):\n    return x\n", 1),
    ("param_spec", "def f[**P](x):\n    return x\n", 1),
    ("type_var_tuple", "def f[*Ts](x):\n    return x\n", 1),
    ("several", "def f[T, **P, *Ts](x):\n    return x\n", 1),
    ("nested", "def g():\n    def f[T](x):\n        return x\n    return f\n", 2),
    ("in_loop", "for i in range(2):\n    def f[T](x):\n        return x\n", 2),
]


@pytest.mark.parametrize(
    ("source", "line"),
    [(s, n) for _, s, n in FUNCTION_TYPE_PARAMS],
    ids=[i for i, _, _ in FUNCTION_TYPE_PARAMS],
)
def test_function_type_parameters_are_rejected(source: str, line: int) -> None:
    message = str(_rejection(source))
    assert "type parameters on function 'f' are not allowed" in message
    assert "drop the `[...]` type parameters" in message
    assert f"(line {line}," in message


@pytest.mark.skipif(sys.version_info < (3, 13), reason="type parameter defaults are 3.13 syntax")
def test_function_type_parameter_default_is_rejected() -> None:
    message = str(_rejection("def f[T = int](x):\n    return x\n"))
    assert "type parameters on function 'f' are not allowed" in message


CLASS_TYPE_PARAMS = [
    ("env_schema", "from atlantide.core import EnvSchema\nclass E[T](EnvSchema):\n    a: int\n"),
    ("bound", "from atlantide.core import EnvSchema\nclass E[T: int](EnvSchema):\n    a: int\n"),
    ("param_spec", "from atlantide.core import EnvSchema\nclass E[**P](EnvSchema):\n    a: int\n"),
    ("bare", "class E[T]:\n    pass\n"),
]


@pytest.mark.parametrize(
    "source", [s for _, s in CLASS_TYPE_PARAMS], ids=[i for i, _ in CLASS_TYPE_PARAMS]
)
def test_class_type_parameters_are_rejected(source: str) -> None:
    message = str(_rejection(source))
    assert "type parameters on class 'E' are not allowed" in message
    assert "drop the `[...]` type parameters" in message


def test_a_generic_env_schema_would_otherwise_be_accepted() -> None:
    """The class rejection above is due to ``[T]`` alone: without it the schema is valid."""
    source = "from atlantide.core import EnvSchema\nclass E(EnvSchema):\n    a: int\n"
    assert is_successful(validate_source(source))


@pytest.mark.parametrize(
    "source",
    [
        "type X = int\nx = 1\n",
        "def f[T](x):\n    return x\ny = f(1)\n",
        "from atlantide.core import EnvSchema\nclass E[T](EnvSchema):\n    a: int\n",
    ],
    ids=["type_alias", "generic_function", "generic_class"],
)
def test_evaluation_refuses_before_running_anything(source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.mark.parametrize("node", ["TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple"])
def test_pep695_nodes_are_outside_the_allowed_subset(node: str) -> None:
    assert hasattr(ast, node), f"ast.{node} exists on every supported Python"
    assert node not in ALLOWED_NODES


def test_every_node_with_type_params_is_checked() -> None:
    """Only ``def``, ``class`` and ``type`` carry type parameters (no lambdas, no async
    defs reach the check: ``AsyncFunctionDef`` is rejected outright)."""
    carriers = {
        name
        for name in dir(ast)
        if isinstance(getattr(ast, name), type)
        and issubclass(getattr(ast, name), ast.AST)
        and "type_params" in getattr(ast, name)._fields
    }
    assert carriers == {"FunctionDef", "AsyncFunctionDef", "ClassDef", "TypeAlias"}
    assert "AsyncFunctionDef" not in ALLOWED_NODES


@pytest.mark.parametrize(
    "source",
    ["x = dict(type=1)\n", "x = {'type': 1}\n", "x = [1]\ny = x[0]\n"],
    ids=["keyword", "dict_key", "subscript"],
)
def test_type_soft_keyword_leaves_ordinary_code_alone(source: str) -> None:
    """``type`` became a soft keyword; the spellings config already used still validate."""
    assert is_successful(validate_source(source))


def test_type_as_a_name_is_still_refused_as_the_builtin() -> None:
    message = str(_rejection("type = 1\n"))
    assert "name 'type' is not allowed" in message
