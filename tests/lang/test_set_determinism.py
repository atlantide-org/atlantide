"""A set's order never reaches config output, however it is rendered or iterated.

Rendering or iterating a set natively follows its hash order, which depends on
``PYTHONHASHSEED`` for strings. Anything config renders can land in a resource
field and from there in the hashed IR, so every text-producing path (``str``,
f-strings with or without a conversion or spec, ``%``, a nested set, a dict
view) and every iteration path must use the one deterministic order: elements
sorted by their canonical ``repr``.

Each case below runs under several hash seeds, each in a fresh interpreter, and
must produce the same text and the same IR hash every time — and the text
Python itself would print, with the set's elements in that order.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from returns.pipeline import is_successful

from atlantide.core import FuelExhaustedError
from atlantide.lang import evaluate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope
from atlantide.lang.interp.stability import stable_form, stable_repr
from atlantide.lang.validate import validate_source

_REPO = Path(__file__).resolve().parents[2]

#: A fixed seed or two plus a fresh one per run: the fixed ones reproduce, the
#: random one keeps looking.
_SEEDS = ("0", "1", "2", "3", "42", "1337", str(random.randrange(1, 2**32 - 1)))

WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")
S = "{" + ", ".join(repr(w) for w in WORDS) + "}"
FS = f"frozenset({S})"
S_INDIA = "{" + ", ".join(repr(w) for w in (*WORDS, "india")) + "}"
FS_NO_ALPHA = "frozenset({" + ", ".join(repr(w) for w in WORDS[1:]) + "})"

_PRELUDE = (
    "s = {'delta', 'hotel', 'alpha', 'golf', 'charlie', 'echo', 'bravo', 'foxtrot'}\n"
    "fs = frozenset(s)\n"
)

#: name -> (config setting `x`, the text `x` must hold). The config also
#: declares `Box('b', label=x)`, so the text is part of the IR being hashed.
RENDER: dict[str, tuple[str, str]] = {
    # f-strings
    "fstring": ("x = f'{s}'", S),
    "fstring_nested": ("x = f'{[s]}'", f"[{S}]"),
    "fstring_repr": ("x = f'{s!r}'", S),
    "fstring_str_nested": ("x = f'{(s,)!s}'", f"({S},)"),
    "fstring_ascii": ("e = {'é', 'z', 'a'}\nx = f'{[e]!a}'", "[{'a', 'z', '\\xe9'}]"),
    "fstring_empty_spec": ("x = f'{[s]:}'", f"[{S}]"),
    "fstring_set_empty_spec": ("x = f'{fs:}'", FS),
    "fstring_repr_then_spec": ("x = f'{fs!r:>120}'", FS.rjust(120)),
    # the str builtin
    "str": ("x = str(s)", S),
    "str_frozenset": ("x = str(fs)", FS),
    "str_list": ("x = str([s])", f"[{S}]"),
    "str_tuple": ("x = str((s,))", f"({S},)"),
    "str_dict_value": ("x = str({'k': s})", f"{{'k': {S}}}"),
    "str_frozenset_key": ("x = str({fs: 1})", f"{{{FS}: 1}}"),
    "str_tuple_key": ("x = str({(1, fs): 'v'})", f"{{(1, {FS}): 'v'}}"),
    "str_deep": ("x = str([[{'k': [s]}]])", f"[[{{'k': [{S}]}}]]"),
    "str_empty": (
        "x = str([set(), frozenset(), {frozenset()}])",
        "[set(), frozenset(), {frozenset()}]",
    ),
    "frozenset_of_frozensets": (
        "x = str(frozenset({fs, frozenset({'y', 'x'}), frozenset()}))",
        f"frozenset({{frozenset(), {FS}, frozenset({{'x', 'y'}})}})",
    ),
    "set_of_tuples": ("x = str({('b', 2), ('a', 1), ('c', 3)})", "{('a', 1), ('b', 2), ('c', 3)}"),
    "mixed_types": ("x = str({10, 9, 'a', None, 2.5, ('t',)})", "{'a', ('t',), 10, 2.5, 9, None}"),
    "set_algebra": ("x = str([s | {'india'}, fs - {'alpha'}])", f"[{S_INDIA}, {FS_NO_ALPHA}]"),
    # dict views
    "dict_values": ("x = str({'g': s}.values())", f"dict_values([{S}])"),
    "dict_keys": ("x = str({fs: 1}.keys())", f"dict_keys([{FS}])"),
    "dict_items": ("x = str({'k': s}.items())", f"dict_items([('k', {S})])"),
    # printf-style
    "percent_tuple": ("x = '%s' % (s,)", S),
    "percent_single": ("x = '%s' % s", S),
    "percent_list": ("x = '%s' % [s]", f"[{S}]"),
    "percent_repr": ("x = '%r|%s' % (fs, [s])", f"{FS}|[{S}]"),
    "percent_mapping": ("x = '%(k)s' % {'k': s}", S),
    "percent_mapping_whole": ("x = '%s' % {'k': s}", f"{{'k': {S}}}"),
    "percent_ascii": ("x = '%a' % ({'é', 'a'},)", "{'a', '\\xe9'}"),
    "percent_bytes": ("x = (b'%r' % ([s],)).decode()", f"[{S}]"),
    # natives that render
    "join": ("x = '-'.join(s) + '|' + '-'.join(fs)", "-".join(WORDS) + "|" + "-".join(WORDS)),
    "map_str": ("x = '|'.join(map(str, [s, fs]))", f"{S}|{FS}"),
    "sorted_key_str": ("x = str(sorted([{'zz'}, s], key=str))", f"[{S}, {{'zz'}}]"),
    "to_json": ("x = to_json(s)", json.dumps(list(WORDS), separators=(",", ":"))),
}

ITERATE: dict[str, tuple[str, str]] = {
    "for_loop": ("x = ''\nfor w in s:\n    x += w[0]", "abcdefgh"),
    "list_comp": ("x = str([w for w in s])", str(list(WORDS))),
    "generator_join": ("x = ','.join(w.upper() for w in fs)", ",".join(w.upper() for w in WORDS)),
    "dict_comp": ("x = str({w: len(w) for w in s})", str({w: len(w) for w in WORDS})),
    "set_comp": (
        "x = str({w[-2:] for w in s})",
        "{" + ", ".join(sorted(repr(w[-2:]) for w in WORDS)) + "}",
    ),
    "nested_comp": (
        "x = str([a + b for a in {'y', 'x'} for b in {'2', '1'}])",
        "['x1', 'x2', 'y1', 'y2']",
    ),
    "list_tuple": ("x = str(list(s)) + str(tuple(fs))", str(list(WORDS)) + str(WORDS)),
    "sorted_min_max": (
        "x = str([sorted(s), min(s), max(s)])",
        str([list(WORDS), "alpha", "hotel"]),
    ),
    "max_key_tie": ("x = max(s, key=len) + min(s, key=len)", "charlieecho"),
    "filter": (
        "x = str(list(filter(lambda w: 'o' in w, s)))",
        "['bravo', 'echo', 'foxtrot', 'golf', 'hotel']",
    ),
    "map": ("x = str(list(map(lambda w: w[1], s)))", str([w[1] for w in WORDS])),
    "enumerate_zip": (
        "x = str(list(enumerate(s))[:2]) + str(list(zip(s, fs))[-1])",
        "[(0, 'alpha'), (1, 'bravo')]('hotel', 'hotel')",
    ),
    "unpack": ("a, b, c = {'c', 'a', 'b'}\nx = a + b + c", "abc"),
    "star_call": ("def f(a, b, c):\n    return a + b + c\nx = f(*{'c', 'b', 'a'})", "abc"),
    "extend": ("xs = []\nxs.extend(s)\nx = str(xs)", str(list(WORDS))),
    "pop": ("t = set(s)\nx = t.pop() + t.pop() + str(len(t))", "alphabravo6"),
    "pop_unbound": ("t = set(s)\nx = set.pop(t) + str(len(t))", "alpha7"),
    "set_of_sets": (
        "x = '|'.join(str(e) for e in {fs, frozenset({'y', 'x'})})",
        f"{FS}|frozenset({{'x', 'y'}})",
    ),
    "resource_nested_set": ("Tagged('t', groups={'k': s}, rows=[fs])\nx = 'tagged'", "tagged"),
}

#: Failures must be deterministic too: same message under every seed.
FAIL: dict[str, tuple[str, str]] = {
    "spec_on_set": ("x = f'{s:>5}'", "unsupported format string passed to set.__format__"),
    "percent_d_on_set": ("x = '%d' % (s,)", "a real number is required, not set"),
    "address_in_set": ("x = f'{[{to_json}]}'", "memory layout"),
}

_PROG = r"""
import json, sys
from typing import ClassVar
from returns.pipeline import is_successful
from atlantide.core import ProviderRegistry, Resource, mutable
from atlantide.ir import hash_ir, lower
from atlantide.lang import evaluate_source
from tests.support import Box, FakeProvider

class Tagged(Resource):
    class Meta:
        provider: ClassVar[str] = "test"
    groups: dict[str, list[str]] = mutable(default_factory=dict)
    rows: list[list[str]] = mutable(default_factory=list)

providers = ProviderRegistry()
providers.register(FakeProvider())
out = {}
for name, src in json.loads(sys.stdin.read()).items():
    result = evaluate_source(
        src + "\nBox('b', size=1, label=x)\n", extra_globals={"Box": Box, "Tagged": Tagged}
    )
    if not is_successful(result):
        out[name] = ["error", str(result.failure())]
        continue
    registry = result.unwrap()
    label = next(r.label for r in registry.all() if isinstance(r, Box))
    out[name] = [label, hash_ir(lower(registry, providers))]
print(json.dumps(out))
"""


def _base_env() -> dict[str, str]:
    keep = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "HOME")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _run(seed: str, cases: dict[str, str]) -> dict[str, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROG],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        cwd=_REPO,
        env={**_base_env(), "PYTHONHASHSEED": seed},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def by_seed() -> dict[str, dict[str, list[str]]]:
    cases = {name: _PRELUDE + src for name, (src, _) in {**RENDER, **ITERATE, **FAIL}.items()}
    return {seed: _run(seed, cases) for seed in _SEEDS}


def _one_outcome(by_seed: dict[str, dict[str, list[str]]], name: str) -> list[str]:
    outcomes = {seed: runs[name] for seed, runs in by_seed.items()}
    distinct = {json.dumps(o) for o in outcomes.values()}
    assert len(distinct) == 1, f"{name} depends on PYTHONHASHSEED: {outcomes}"
    return next(iter(outcomes.values()))


@pytest.mark.parametrize("name", list(RENDER))
def test_rendering_a_set_is_hash_seed_independent(
    by_seed: dict[str, dict[str, list[str]]], name: str
) -> None:
    text, _ = _one_outcome(by_seed, name)
    assert text == RENDER[name][1]


@pytest.mark.parametrize("name", list(ITERATE))
def test_iterating_a_set_is_hash_seed_independent(
    by_seed: dict[str, dict[str, list[str]]], name: str
) -> None:
    text, _ = _one_outcome(by_seed, name)
    assert text == ITERATE[name][1]


@pytest.mark.parametrize("name", list(FAIL))
def test_a_failure_over_a_set_is_hash_seed_independent(
    by_seed: dict[str, dict[str, list[str]]], name: str
) -> None:
    kind, message = _one_outcome(by_seed, name)
    assert kind == "error"
    assert FAIL[name][1] in message


# -- the renderer, in process --------------------------------------------------

_SET_FREE = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | st.text(max_size=8)
    | st.binary(max_size=4),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.tuples(inner)
        | st.tuples(inner, inner)
        | st.dictionaries(st.text(max_size=4) | st.integers(), inner, max_size=3)
    ),
    max_leaves=16,
)


@given(_SET_FREE)
def test_set_free_data_renders_exactly_as_python_does(value: Any) -> None:
    """Existing output must not move: without a set the renderer is Python's own."""
    assert stable_form(value) is value
    assert stable_repr(value) == repr(value)


def _config_scope(**values: Any) -> Scope:
    return Scope(init={**build_globals(), **values})


@given(_SET_FREE)
def test_set_free_data_renders_in_config_exactly_as_python_does(value: Any) -> None:
    src = "a = str(v)\nb = f'{v!r}'\nc = '%r' % (v,)\nd = f'{[v]}'\ne = f'{v!a}'\n"
    scope = _config_scope(v=value)
    Interpreter().run(validate_source(src).unwrap(), scope)
    assert scope.vars["a"] == str(value)
    assert scope.vars["b"] == scope.vars["c"] == repr(value)
    assert scope.vars["d"] == f"{[value]}"
    assert scope.vars["e"] == ascii(value)


_ELEMENT = st.text(max_size=6) | st.integers() | st.tuples(st.text(max_size=3), st.integers())


@given(st.lists(_ELEMENT, max_size=12, unique=True))
def test_a_set_renders_its_elements_sorted_by_repr(items: list[Any]) -> None:
    forward, backward = set(items), set(reversed(items))
    expected = "{" + ", ".join(sorted(repr(i) for i in forward)) + "}" if forward else "set()"
    assert stable_repr(forward) == stable_repr(backward) == expected
    assert stable_repr(frozenset(items)) == (f"frozenset({expected})" if forward else "frozenset()")
    # Nested, the elements keep the order they have alone.
    assert stable_repr([forward, {"k": frozenset(items)}]) == (
        f"[{expected}, {{'k': {stable_repr(frozenset(items))}}}]"
    )


def test_iteration_order_is_rendering_order() -> None:
    src = (
        "s = {frozenset({'b', 'a'}), frozenset({'c'}), frozenset(), ('z', 1), 'q', 3}\n"
        "joined = ', '.join(str([e])[1:-1] for e in s)\n"
        "whole = str(s)\n"
    )
    scope = _config_scope()
    Interpreter().run(validate_source(src).unwrap(), scope)
    assert scope.vars["whole"] == "{" + scope.vars["joined"] + "}"


@pytest.mark.parametrize(
    "render",
    ["y = str(xs)", "y = f'{xs}'", "y = '%s' % (xs,)", "y = str({'k': xs}.values())"],
)
def test_rendering_nested_sets_costs_fuel(render: str) -> None:
    """Ten references to a 2000-element set are 20000 elements to render."""
    src = f"s = set(range(2000))\nxs = [s, s, s, s, s, s, s, s, s, s]\n{render}\n"
    assert is_successful(evaluate_source(src, fuel=100_000))
    result = evaluate_source(src, fuel=15_000)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)
