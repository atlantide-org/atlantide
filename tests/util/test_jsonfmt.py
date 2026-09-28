"""`atlantide.util.jsonfmt.compact_json` is byte-identical to reference `json.dumps` encodings.

The state codec, the `to_json` builtin and the policy serializers all call the
helper, so comparing against them would be circular. The references below are
explicit `json.dumps` spellings instead; each consumer is checked against its
reference too. This pins that the helper cannot change a byte of a state
document (S3 compare-and-swap compares them), the `to_json` builtin's output (it
flows into the hashed IR) or a policy JSON.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.lang.builtins import to_json
from atlantide.providers.aws.policy import assume_role, policy_document, policy_json
from atlantide.state.codec import _canonical
from atlantide.util.jsonfmt import compact_json


def _state_codec_bytes(value: Any) -> bytes:
    """Reference encoding for `state.codec._canonical`."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _to_json_text(value: Any) -> str:
    """Reference encoding for the `to_json` builtin."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _policy_text(document: Any) -> str:
    """Reference encoding for `policy_json` / `assume_role`."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(st.text(), inner, max_size=5),
    max_leaves=25,
)

_SAMPLES: list[Any] = [
    None,
    True,
    0,
    -1.5,
    "",
    "plain",
    "café",
    "日本語",
    "emoji \U0001f680",
    'quote " backslash \\ newline \n tab \t nul \x00',
    "\u2028\u2029",
    [1, "two", None, [3.0]],
    {"b": 1, "a": {"é": "ü", "z": [], "A": {}}},
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*"}]},
]


@pytest.mark.parametrize("value", _SAMPLES)
def test_samples_match_the_state_codec(value: Any) -> None:
    assert compact_json(value).encode("utf-8") == _state_codec_bytes(value)
    assert _canonical(value) == _state_codec_bytes(value)


@pytest.mark.parametrize("value", _SAMPLES)
def test_samples_match_the_to_json_builtin(value: Any) -> None:
    assert compact_json(value, ascii=False) == _to_json_text(value)
    assert to_json(value) == _to_json_text(value)


def test_ascii_is_the_default_and_escapes() -> None:
    assert compact_json({"k": "é"}) == '{"k":"\\u00e9"}'
    assert compact_json({"k": "é"}, ascii=False) == '{"k":"é"}'


def test_sorted_keys_and_no_whitespace() -> None:
    assert compact_json({"b": [1, 2], "a": {"d": 1, "c": 2}}) == '{"a":{"c":2,"d":1},"b":[1,2]}'


def test_matches_the_policy_serializers() -> None:
    statements: list[Any] = [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::bücket/*"},
        {"Effect": "Deny", "Action": "*", "Resource": "*", "Condition": {"Bool": {"x": "y"}}},
    ]
    assert compact_json(policy_document(statements)) == _policy_text(policy_document(statements))
    assert policy_json(statements) == _policy_text(policy_document(statements))
    trust = policy_document(
        [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ]
    )
    assert compact_json(trust) == _policy_text(trust)
    assert assume_role("lambda.amazonaws.com") == _policy_text(trust)


@given(_json_values)
def test_any_value_matches_the_state_codec(value: Any) -> None:
    assert compact_json(value).encode("utf-8") == _state_codec_bytes(value)
    assert _canonical(value) == _state_codec_bytes(value)


@given(_json_values)
def test_any_value_matches_the_to_json_builtin(value: Any) -> None:
    assert compact_json(value, ascii=False) == _to_json_text(value)
    assert to_json(value) == _to_json_text(value)
