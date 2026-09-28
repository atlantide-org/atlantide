"""Regression tests: canonical encoder errors on mixed keys and lone surrogates."""

from __future__ import annotations

import pytest

from atlantide.core import IRError
from atlantide.ir import to_canonical_json


@pytest.mark.parametrize("obj", [{"a": 1, 2: "b"}, {1: "a", "b": 2}, {None: 1, "a": 2}])
def test_mixed_type_keys_raise_ir_error_not_type_error(obj: dict[object, object]) -> None:
    with pytest.raises(IRError, match="is not a string"):
        to_canonical_json({"outer": obj})


def test_a_lone_surrogate_value_raises_ir_error_with_its_path() -> None:
    with pytest.raises(IRError, match=r"string at <root>\.a\[1\] contains a lone surrogate"):
        to_canonical_json({"a": ["ok", "bad\ud800"]})


def test_a_lone_surrogate_key_raises_ir_error() -> None:
    with pytest.raises(IRError, match=r"string at <root>\.x\.'\\udc80' contains a lone surrogate"):
        to_canonical_json({"x": {"\udc80": 1}})


def test_a_lone_surrogate_at_the_root_raises_ir_error() -> None:
    with pytest.raises(IRError, match=r"string at <root> contains"):
        to_canonical_json("\ud800")


def test_astral_text_still_encodes() -> None:
    assert to_canonical_json({"k": "\U0001f600"}) == '{"k":"\U0001f600"}'.encode()
