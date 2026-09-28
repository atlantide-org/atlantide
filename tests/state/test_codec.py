"""The snapshot and journal-entry encodings: canonical, versioned, strict on input."""

from __future__ import annotations

import json

import pytest

from atlantide.core.errors import StateError
from atlantide.state import StateNode
from atlantide.state.codec import (
    SNAPSHOT_VERSION,
    EntryKind,
    EntryOp,
    JournalEntry,
    StateDocument,
    decode,
    decode_entry,
    dumps,
    encode,
    encode_entry,
    loads,
)


def _doc() -> StateDocument:
    node = StateNode(
        id="a",
        type="test.T",
        provider="test",
        provider_version="1.0.0",
        input_hash="h0",
        outputs={"arn": "arn::a"},
        properties={"n": 3},
        dependencies=("x", "y"),
        prevent_destroy=True,
        status="creating",
        secret_digests={"password": "deadbeef"},
        ref_digests={"n": "sha256:beef"},
    )
    return StateDocument(serial=7, nodes={"a": node}, outputs={"dev:url": "u"})


def test_roundtrip_preserves_every_field() -> None:
    original = _doc()
    assert loads(dumps(original)) == original


def test_a_row_written_before_the_ref_record_reads_as_unrecorded() -> None:
    """Snapshots and journal entries from a build without ``ref_digests`` load
    with an empty record, which the diff treats as unknown."""
    payload = json.loads(dumps(_doc()))
    del payload["nodes"]["a"]["ref_digests"]
    assert loads(json.dumps(payload).encode()).nodes["a"].ref_digests == {}
    entry = json.loads(
        encode_entry(
            JournalEntry(
                kind=EntryKind.NODE,
                name="a",
                seq=1,
                fence=1,
                op=EntryOp.PUT,
                record=_doc().nodes["a"],
            )
        )
    )
    del entry["record"]["ref_digests"]
    decoded = decode_entry(json.dumps(entry).encode())
    assert decoded.record is not None and decoded.record.ref_digests == {}


def test_encoding_is_canonical() -> None:
    """Byte-identical output for equal state, so two blobs diff meaningfully."""
    raw = dumps(_doc())
    assert raw == dumps(loads(raw))
    assert b" " not in raw.replace(b'"dev:url"', b"")  # compact separators


def test_empty_document_roundtrips() -> None:
    assert loads(dumps(StateDocument())) == StateDocument()


def test_future_version_is_refused() -> None:
    raw = json.dumps({"version": SNAPSHOT_VERSION + 1, "serial": 0, "nodes": {}}).encode()
    with pytest.raises(StateError, match="upgrade atlantide"):
        loads(raw)


@pytest.mark.parametrize("version", [1, 2])
def test_an_older_format_is_refused_with_the_way_forward(version: int) -> None:
    """No in-place conversion: the error names `state migrate` as the path."""
    raw = json.dumps({"version": version, "serial": 3, "nodes": {}, "outputs": {}}).encode()
    with pytest.raises(StateError, match="state migrate"):
        loads(raw)


def test_the_journal_bookkeeping_round_trips() -> None:
    doc = StateDocument(
        serial=1,
        fences={"s:t:a": 7},
        max_fence=9,
        wm={"s:t:a": 4},
        owm={"s": 2},
        gen=3,
        epoch="e1",
    )
    assert loads(dumps(doc)) == doc


def test_a_future_journal_format_is_refused() -> None:
    raw = json.loads(dumps(StateDocument()))
    raw["journal"]["format"] = 99
    with pytest.raises(StateError, match="upgrade atlantide"):
        loads(json.dumps(raw).encode())


@pytest.mark.parametrize("version", [True, 0, "3", None])
def test_an_unknown_version_is_refused(version: object) -> None:
    raw = json.dumps({"version": version, "serial": 0, "nodes": {}}).encode()
    with pytest.raises(StateError, match="format version"):
        loads(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b"[]",
        json.dumps({"version": SNAPSHOT_VERSION}).encode(),  # no serial
        json.dumps(
            {"version": SNAPSHOT_VERSION, "serial": 0, "nodes": {"a": {"id": "a"}}}
        ).encode(),  # node missing required fields
        json.dumps(
            {"version": SNAPSHOT_VERSION, "serial": 0, "fences": {"a": "7"}}
        ).encode(),  # a fence that is not an integer
        json.dumps({"version": SNAPSHOT_VERSION, "serial": 0, "max_fence": "1"}).encode(),
        json.dumps({"version": SNAPSHOT_VERSION, "serial": 0, "gen": True}).encode(),
        json.dumps({"version": SNAPSHOT_VERSION, "serial": 0, "journal": []}).encode(),
        json.dumps({"version": SNAPSHOT_VERSION, "serial": 0, "journal": {"epoch": 1}}).encode(),
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
                id=f"n{i}",
                type="test.T",
                provider="test",
                provider_version="1.0.0",
                input_hash="h0",
                outputs={"arn": f"arn::{i}"},
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


# -- journal entries --------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        JournalEntry(
            EntryKind.NODE, "s:t:a", 3, 7, EntryOp.PUT, owner="me", record=_doc().nodes["a"]
        ),
        JournalEntry(EntryKind.NODE, "s:t:a", 4, 7, EntryOp.DELETE),
        JournalEntry(EntryKind.OUTPUT, "s", 1, 0, EntryOp.PUT, outputs={"s:url": "u"}),
        JournalEntry(EntryKind.OUTPUT, "s", 2, 0, EntryOp.DELETE),
    ],
)
def test_an_entry_round_trips(entry: JournalEntry) -> None:
    assert decode_entry(encode_entry(entry)) == entry
    assert decode_entry(encode_entry(entry, compress_over=0)) == entry


@pytest.mark.parametrize(
    "raw",
    [
        b"nope",
        json.dumps({"v": 2}).encode(),
        json.dumps({"v": 1, "kind": "log"}).encode(),
        json.dumps(
            {"v": 1, "kind": "zzz", "op": "put", "name": "a", "seq": 1, "fence": 0}
        ).encode(),
        json.dumps(
            {"v": 1, "kind": "log", "op": "put", "name": "a", "seq": "1", "fence": 0}
        ).encode(),
    ],
)
def test_a_corrupt_entry_is_refused(raw: bytes) -> None:
    with pytest.raises(StateError):
        decode_entry(raw)
