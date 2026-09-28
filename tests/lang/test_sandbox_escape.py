"""The sandbox boundary: what config must not be able to reach.

Each test names the capability being denied (environment, disk, subprocess, the
interpreter's own globals) rather than the syntax that reaches it, so the file
states what the subset rules in :mod:`atlantide.lang.validate` guarantee.
"""

from __future__ import annotations

import pytest

from atlantide.core import FuelExhaustedError, LanguageError, is_successful
from atlantide.core.errors import LanguageError as CoreLanguageError
from atlantide.core.types import format_template, interpolate
from atlantide.lang import evaluate_source, validate_source
from tests.lang.test_interp import Widget

#: (capability, source). Denied by the import allow-list: each target module is
#: ordinary unsandboxed Python importing `os`, `pathlib`, or `subprocess`, and the
#: interpreter binds what it finds there into config scope as a live callable.
ESCAPES = [
    ("environ", "from atlantide.secrets.env import EnvSecretsProvider"),
    ("file_read", "from atlantide.providers.local.provider import _read_content"),
    ("subprocess", "from atlantide.components.fetch import _git"),
    ("secret_store", "from atlantide.secrets.keyfile_store import KeyfileValueStore"),
    ("state_write", "from atlantide.state.sql.sqlite import SqliteStateBackend"),
    ("cli", "from atlantide.cli.main import app"),
    ("component_lock", "from atlantide.components.lock import load_lock"),
    ("aws_handler", "from atlantide.providers.aws.handlers.s3 import S3BucketHandler"),
    ("private_name", "from atlantide.core.markers import _tree"),
]


@pytest.mark.parametrize(("capability", "source"), ESCAPES, ids=[c for c, _ in ESCAPES])
def test_import_escapes_are_rejected(capability: str, source: str) -> None:
    result = validate_source(source)
    assert not is_successful(result), f"{capability} escape should be rejected"
    assert isinstance(result.failure(), LanguageError)


def test_provider_classes_are_not_importable_by_config() -> None:
    """`atlantide.providers.aws` is allow-listed for its resource types, but a
    Provider is the object holding the boto3 calls."""
    result = evaluate_source("from atlantide.providers.aws import AwsProvider\nx = 1")
    assert not is_successful(result)
    assert "provider" in str(result.failure()).lower()


def test_interpreter_rejects_a_denied_module_even_without_validation() -> None:
    """The allow-list is re-checked where the name is bound.

    `import_module` executes the target and `getattr` hands config a live
    callable, so `_st_ImportFrom` cannot rely on having been validated first.
    """
    from atlantide.lang.builtins import build_globals
    from atlantide.lang.interp import Interpreter, Scope

    tree = __import__("ast").parse("from atlantide.secrets.env import EnvSecretsProvider")
    with pytest.raises(CoreLanguageError):
        Interpreter().run(tree, Scope(build_globals({})))


# -- format-string traversal ------------------------------------------------
#
# `"{0.__class__.__init__.__globals__[x]}".format(obj)` walks attributes on a
# live object. The template is an `ast.Constant`, so the dunder ban on Name and
# Attribute nodes does not see it.

_TRAVERSAL = "{0.__class__.__init__.__globals__[__builtins__]}"


def test_str_format_is_not_callable_from_config() -> None:
    result = validate_source(f"x = '{_TRAVERSAL}'.format(1)")
    assert not is_successful(result)
    assert "format" in str(result.failure())


def test_interpolate_rejects_a_traversing_template_at_config_time() -> None:
    with pytest.raises(CoreLanguageError):
        interpolate(_TRAVERSAL, object())


@pytest.mark.parametrize("template", [_TRAVERSAL, "{0[0]}", "{0.real}"])
def test_format_template_rejects_field_access(template: str) -> None:
    """The apply-time reducer is the second half: a `$transform` marker read back
    from state has not passed `interpolate`'s config-time check."""
    with pytest.raises(CoreLanguageError):
        format_template(template, object())


def test_format_template_still_substitutes_positionally() -> None:
    assert format_template("{}/img/{}", "cdn", "logo.png") == "cdn/img/logo.png"
    assert format_template("{1}-{0}", "b", "a") == "a-b"


def test_format_template_reports_a_missing_argument() -> None:
    with pytest.raises(CoreLanguageError):
        format_template("{0}/{1}", "only-one")


# -- silently discarded arguments -------------------------------------------
#
# Not a sandbox hole but the same failure shape: the config compiles, and the
# plan matches what was written minus the dropped values, with no diagnostic.
# `Bucket("assets", bucket=name, **common_tags)` yields a bucket with no tags.

DISCARDED = [
    ("kwargs_unpacking", "def h(a=1):\n    return a\nx = h(**{'a': 9})"),
    ("surplus_positional", "def f(a):\n    return a\nx = f(1, 2, 3)"),
]


@pytest.mark.parametrize(("name", "source"), DISCARDED, ids=[n for n, _ in DISCARDED])
def test_silently_discarded_arguments_are_rejected(name: str, source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result), f"{name} should be rejected"


@pytest.mark.parametrize(
    "source",
    [
        "def g(a, b=2):\n    return a + b\nx = g(1)",
        "def g(a, b=2):\n    return a + b\nx = g(1, 5)",
        "def g(a, b=2):\n    return a + b\nx = g(1, b=5)",
        "def g(a, b=2):\n    return a + b\nx = g(*[1, 5])",
    ],
)
def test_ordinary_calls_still_work(source: str) -> None:
    assert is_successful(evaluate_source(source))


# -- the one class config may declare ---------------------------------------
#
# Atlas-lang admits a `class X(EnvSchema)` whose body is only annotated fields.
# The validator checks that shape syntactically, leaving two holes only the
# interpreter can close — one per guard below.


def test_a_rebound_base_cannot_smuggle_in_another_class() -> None:
    """The validator matches the *spelling* `EnvSchema`, so this passes it.

    Without the interpreter's identity check, `type(name, (S3Bucket,), ns)` would
    run pydantic's metaclass over a namespace the config controls.
    """
    source = (
        "from atlantide.providers.aws import S3Bucket\n"
        "EnvSchema = S3Bucket\n"
        "class Evil(EnvSchema):\n"
        "    region: str\n"
    )
    assert is_successful(validate_source(source)), "the validator cannot see a rebind"
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "rebound" in str(result.failure())


def test_a_default_never_becomes_a_class_attribute() -> None:
    """`type()` runs `__set_name__` on a direct namespace value but not on one
    nested in a dict, and a default is an arbitrary config expression. Keeping
    defaults in `__atlas_defaults__` stops a default's own code running as the
    class is built, and stops it shadowing each environment's value."""
    source = (
        "from atlantide.core import Config, EnvSchema, Stack, output\n"
        "class E(EnvSchema):\n"
        "    region: str\n"
        "    tier: str = 'small'\n"
        "config = Config(E, envs={'dev': {'region': 'r'}, "
        "'prod': {'region': 'r', 'tier': 'large'}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        output('tier', env.tier)\n"
    )
    outputs = evaluate_source(source).unwrap().outputs
    # 'prod' reading 'small' would mean the class attribute won the lookup.
    assert outputs == {"dev:tier": "small", "prod:tier": "large"}, outputs


def test_a_schema_class_cannot_be_declared_inside_a_loop() -> None:
    """A schema is a declaration, not a computation: a per-iteration class has no
    defined meaning."""
    source = (
        "from atlantide.core import EnvSchema\n"
        "for i in [1, 2]:\n"
        "    class E(EnvSchema):\n"
        "        region: str\n"
    )
    assert not is_successful(validate_source(source))


# -- the surface config legitimately needs must keep working ----------------

ALLOWED = [
    "from atlantide.core import Stack, output",
    "from atlantide.policy import enforce",
    "from atlantide.providers.aws import S3Bucket, SecureBucket",
    "from atlantide.providers.random import Id",
    "from atlantide.components.acme import SecureSite",
]


@pytest.mark.parametrize("source", ALLOWED)
def test_config_surface_is_still_importable(source: str) -> None:
    assert is_successful(validate_source(source))


# -- attribute policy -------------------------------------------------------
#
# A plain `getattr` with only dunders refused would make everything pydantic
# hangs off a resource class config API: `Widget.parse_file("/etc/hosts")` reads
# the disk (and `/dev/zero` reads until memory runs out). Leading-underscore
# state on `atlantide` would bypass input tracking, and a user function's
# `interp` is the evaluator itself.

ATTRIBUTE_ESCAPES = [
    ("parse_file", "data = Widget.parse_file('/etc/hosts')"),
    ("model_validate_json", "data = Widget.model_validate_json('{}')"),
    ("model_fields", "f = Widget.model_fields"),
    ("model_construct", "w = Widget.model_construct(size=1)"),
    ("instance_model_dump", "w = Widget('w', size=1)\nd = w.model_dump()"),
    ("private_inputs", "x = atlantide._inputs"),
    ("private_resource_state", "w = Widget('w', size=1)\ns = w._stack"),
]


@pytest.mark.parametrize(
    ("name", "source"), ATTRIBUTE_ESCAPES, ids=[n for n, _ in ATTRIBUTE_ESCAPES]
)
def test_attribute_escapes_are_rejected_statically(name: str, source: str) -> None:
    result = validate_source(source)
    assert not is_successful(result), f"{name} should be rejected by the validator"


RUNTIME_ATTRIBUTE_ESCAPES = [
    # Plausible field names, so only the interpreter (knowing the model) refuses.
    ("instance_json", "w = Widget('w', size=1)\nx = w.json()"),
    ("class_schema", "x = Widget.schema()"),
    ("class_copy", "x = Widget.copy"),
    # A user function's attributes are the evaluator: `f.interp.fuel`, `f.scope`.
    ("closure_interp", "def f():\n    return 1\ni = f.interp"),
    ("closure_scope", "g = lambda: 1\ns = g.scope"),
    # Mutable bookkeeping is not public.
    ("consumed", "atlantide.consumed['x'] = 1"),
]


@pytest.mark.parametrize(
    ("name", "source"), RUNTIME_ATTRIBUTE_ESCAPES, ids=[n for n, _ in RUNTIME_ATTRIBUTE_ESCAPES]
)
def test_attribute_escapes_are_rejected_at_runtime(name: str, source: str) -> None:
    result = evaluate_source(source, extra_globals={"Widget": Widget})
    assert not is_successful(result), f"{name} should be rejected"


def test_the_interpreter_applies_the_attribute_policy_without_validation() -> None:
    """`validate` sees spellings; the interpreter re-checks the attribute read."""
    from atlantide.lang.builtins import build_globals
    from atlantide.lang.interp import Interpreter, Scope

    tree = __import__("ast").parse("x = Widget.parse_file('/etc/hosts')")
    with pytest.raises(CoreLanguageError, match="parse_file"):
        Interpreter().run(tree, Scope(init={**build_globals({}), "Widget": Widget}))


def test_reading_an_input_is_still_recorded_and_private_state_is_not_reachable() -> None:
    reg = evaluate_source(
        "Widget('w', size=atlantide.input('n'))",
        inputs={"n": 3, "unread": 1},
        extra_globals={"Widget": Widget},
    ).unwrap()
    assert reg.inputs == {"n": 3}


def test_declared_fields_and_resource_outputs_stay_readable() -> None:
    source = (
        "a = Widget('a', size=2, label='x')\n"
        "Widget('b', size=a.size * 2, label=a.label + str(a.node_id))\n"
    )
    reg = evaluate_source(source, extra_globals={"Widget": Widget}).unwrap()
    b = reg.get("default:test.Widget:b").unwrap()
    assert (b.size, b.label) == (4, "xdefault:test.Widget:a")


# -- resource bounds --------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # Repeated squaring: 2**(2**30), 544MB if uncapped.
        "x = 2\nfor i in range(30):\n    x = x * x",
        "x = 2\nfor i in range(30):\n    x *= x",
        "x = 1 << 10**9",
        "x = (10**300) ** 1000",
    ],
    ids=["square", "aug_square", "lshift", "power_of_power"],
)
def test_integer_growth_is_bounded(source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)
    assert "bits" in str(result.failure())


def test_ordinary_integer_arithmetic_is_unaffected() -> None:
    reg = evaluate_source(
        "Widget('w', size=(2**64 - 1) // 3 % 1000 + (1 << 20) * 3)",
        extra_globals={"Widget": Widget},
    ).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().size == (2**64 - 1) // 3 % 1000 + (1 << 20) * 3


#: `x = [x, x]` sixty times is 61 objects but 2**60 nodes to anything that walks
#: it natively: 1.5GB from `to_json` unless metered by nodes visited.
_DAG = "x = [0]\nfor i in range(60):\n    x = [x, x]\n"
_TUPLE_DAG = "t = (0,)\nfor i in range(60):\n    t = (t, t)\n"


@pytest.mark.parametrize(
    "source",
    [
        _DAG + "s = to_json(x)",
        _DAG + "s = str(x)",
        _DAG + "s = f'{x}'",
        _DAG + "s = f'{x!r}'",
        _DAG + "s = merge({'a': x}, {'b': 1})",
        _DAG + "y = [0]\nfor i in range(60):\n    y = [y, y]\nb = x == y",
        _TUPLE_DAG + "s = {t}",
        _TUPLE_DAG + "d = {t: 1}",
        "x = 'a'\nfor i in range(40):\n    x = x + x",
        "x = [0]\nfor i in range(40):\n    x = x + x",
    ],
    ids=[
        "to_json",
        "str",
        "fstring",
        "fstring_repr",
        "merge",
        "eq",
        "set_hash",
        "dict_key_hash",
        "str_concat",
        "list_concat",
    ],
)
def test_native_walks_are_metered_by_nodes_visited(source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


# -- determinism of rendered text -------------------------------------------
#
# A default repr embeds a memory address, so text built from one differs per run
# and so does the IR hash of any field it reaches.

ADDRESS_LEAKS = [
    ("fstring_repr_api", "x = f'{to_json!r}'"),
    ("str_function", "x = str(merge)"),
    ("str_closure", "def f():\n    return 1\nx = str([f])"),
    ("map_str", "x = ','.join(map(str, [to_json]))"),
    ("sorted_key_str", "x = sorted([to_json, merge], key=str)"),
    ("percent_format", "x = '%r' % (to_json,)"),
    ("set_of_functions", "x = [1 for f in {to_json, merge}]"),
]


@pytest.mark.parametrize(("name", "source"), ADDRESS_LEAKS, ids=[n for n, _ in ADDRESS_LEAKS])
def test_address_bearing_text_is_refused(name: str, source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result), f"{name} should be rejected"
    assert "memory layout" in str(result.failure())


def test_the_config_api_renders_deterministically() -> None:
    reg = evaluate_source(
        "Widget('w', size=1, label=f'{atlantide!r}')", extra_globals={"Widget": Widget}
    ).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().label == "<atlantide config API>"


def test_data_and_handles_still_render() -> None:
    source = (
        "a = Widget('a', size=1)\n"
        "Widget('b', size=1, label=f'{[1, 2.5, None, True]}|{ {\"k\": (1,)} }|{str}|{a.size}')\n"
    )
    reg = evaluate_source(source, extra_globals={"Widget": Widget}).unwrap()
    label = reg.get("default:test.Widget:b").unwrap().label
    assert label == "[1, 2.5, None, True]|{'k': (1,)}|<class 'str'>|1"
