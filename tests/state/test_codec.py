"""The whole-graph blob encoding: canonical, versioned, and strict on input."""

from __future__ import annotations

import json

import pytest

from atlantide.core.errors import StateError
from atlantide.state import StateNode
from atlantide.state.codec import (
    DOCUMENT_VERSION,
    StateDocument,
    decode,
    dumps,
    encode,
    loads,
)


def _doc() -> StateDocument:
    node = StateNode(
        id="a", type="test.T", provider="test", provider_version="1.0.0",
        input_hash="h0", outputs={"arn": "arn::a"}, properties={"n": 3},
        dependencies=("x", "y"), prevent_destroy=True, status="creating",
        secret_digests={"password": "deadbeef"},
    )
    return StateDocument(serial=7, nodes={"a": node}, outputs={"dev:url": "u"})


def test_roundtrip_preserves_every_field() -> None:
    original = _doc()
    assert loads(dumps(original)) == original


def test_encoding_is_canonical() -> None:
    """Byte-identical output for equal state, so two blobs diff meaningfully."""
    raw = dumps(_doc())
    assert raw == dumps(loads(raw))
    assert b" " not in raw.replace(b'"dev:url"', b"")  # compact separators


def test_empty_document_roundtrips() -> None:
    assert loads(dumps(StateDocument())) == StateDocument()


def test_future_version_is_refused() -> None:
    raw = json.dumps({"version": DOCUMENT_VERSION + 1, "serial": 0, "nodes": {}}).encode()
    with pytest.raises(StateError, match="upgrade atlantide"):
        loads(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b"[]",
        json.dumps({"version": DOCUMENT_VERSION}).encode(),  # no serial
        json.dumps(
            {"version": DOCUMENT_VERSION, "serial": 0, "nodes": {"a": {"id": "a"}}}
        ).encode(),  # node missing required fields
    ],
)
def test_corrupt_state_is_refused(raw: bytes) -> None:
    with pytest.raises(StateError):
        loads(raw)


def test_large_documents_are_gzipped_and_read_back() -> None:
    doc = StateDocument(
        serial=1,
        nodes={
            f"n{i}": StateNode(
                id=f"n{i}", type="test.T", provider="test", provider_version="1.0.0",
                input_hash="h0", outputs={"arn": f"arn::{i}"},
            )
            for i in range(200)
        },
    )
    body = encode(doc, compress_over=1024)
    assert body[:2] == b"\x1f\x8b"
    assert len(body) < len(dumps(doc))
    assert decode(body) == doc


def test_small_documents_stay_plain_json() -> None:
    body = encode(_doc())
    assert body == dumps(_doc())
    assert decode(body) == _doc()


def test_encoding_is_deterministic() -> None:
    """Identical state must encode identically, or a no-op write can't be skipped."""
    assert encode(_doc()) == encode(_doc())
    big = StateDocument(serial=2, nodes=_doc().nodes)
    assert encode(big, compress_over=1) == encode(big, compress_over=1)


def test_a_corrupt_gzip_stream_is_reported() -> None:
    with pytest.raises(StateError, match="gzip"):
        decode(b"\x1f\x8b" + b"not actually gzip")
