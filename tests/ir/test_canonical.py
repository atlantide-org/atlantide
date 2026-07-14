"""Canonical JSON encoder: determinism and key-order invariance."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core import IRError
from atlantide.ir import to_canonical_json


def test_key_order_invariance() -> None:
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert to_canonical_json(a) == to_canonical_json(b)
    assert to_canonical_json(a) == b'{"a":2,"b":1,"c":{"y":2,"z":1}}'


def test_primitives() -> None:
    assert to_canonical_json(True) == b"true"
    assert to_canonical_json(False) == b"false"
    assert to_canonical_json(None) == b"null"
    assert to_canonical_json(42) == b"42"
    assert to_canonical_json("hi") == b'"hi"'
    assert to_canonical_json(["a", 1, None]) == b'["a",1,null]'


def test_bool_not_confused_with_int() -> None:
    assert to_canonical_json({"x": True}) == b'{"x":true}'
    assert to_canonical_json({"x": 1}) == b'{"x":1}'


def test_rejects_non_finite() -> None:
    with pytest.raises(IRError):
        to_canonical_json(math.nan)
    with pytest.raises(IRError):
        to_canonical_json(math.inf)


def test_rejects_non_string_keys() -> None:
    with pytest.raises(IRError):
        to_canonical_json({1: "a"})


def test_rejects_unknown_type() -> None:
    with pytest.raises(IRError):
        to_canonical_json(object())


# JSON-safe strategy: str keys, primitive/nested values.
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=20,
)


@given(_json)
def test_encoding_is_stable(value: object) -> None:
    assert to_canonical_json(value) == to_canonical_json(value)


@given(st.dictionaries(st.text(), st.integers(), min_size=1, max_size=8))
def test_shuffled_dict_same_encoding(d: dict[str, int]) -> None:
    reversed_d = dict(reversed(list(d.items())))
    assert to_canonical_json(d) == to_canonical_json(reversed_d)
