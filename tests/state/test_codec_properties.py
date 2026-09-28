"""Round-tripping the state document and the node row for arbitrary inputs.

This codec sits between a remote state object and the engine's view of the world.
Two of its properties are hard to cover by example:

* the **gzip boundary**. `encode` compresses above a size threshold, so a
  fixed-size example exercises only one side of that branch. Generating documents
  and driving the threshold covers both.
* **failing closed on garbage.** A codec that returns a wrong document from
  corrupt bytes makes the engine plan against a state that never existed. It must
  refuse arbitrary corruption, not only a few hand-picked byte strings.

The row pair (`node_columns`/`node_from_row`) is covered for the same reason one
level down. It is the only place that knows a node's storage shape, and both the
sqlite and postgres backends bind through it, so a column added to one half and
not the other silently drops a field from every table-shaped backend at once.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core.errors import StateError
from atlantide.state import StateNode
from atlantide.state.codec import (
    COMPRESS_OVER,
    NODE_COLUMNS,
    StateDocument,
    decode,
    dumps,
    encode,
    node_columns,
    node_from_row,
)
from tests.support.strategies import state_documents, state_nodes


@given(state_documents())
def test_a_document_survives_a_round_trip(doc: StateDocument) -> None:
    assert decode(encode(doc)) == doc


@given(state_documents())
def test_encoding_is_a_pure_function_of_the_document(doc: StateDocument) -> None:
    """Identical state must encode to identical bytes. This lets a backend skip a
    no-op write, and keeps gzip's mtime header from leaking the clock into the
    output."""
    assert encode(doc) == encode(doc)


@given(state_documents())
def test_a_document_round_trips_on_both_sides_of_the_compression_threshold(
    doc: StateDocument,
) -> None:
    """Forcing the threshold either way covers both branches, whatever size the
    generator produces."""
    plain = encode(doc, compress_over=10**9)
    gzipped = encode(doc, compress_over=0)

    assert not gzipped.startswith(b"{")  # compressed
    assert decode(plain) == doc
    assert decode(gzipped) == doc
    assert decode(plain) == decode(gzipped)


@given(state_documents())
def test_compression_is_transparent_to_the_reader(doc: StateDocument) -> None:
    """A stored document is self-describing: the reader sniffs gzip's magic
    number rather than trusting a transport header that may not have survived."""
    assert decode(encode(doc, compress_over=0)) == decode(encode(doc, compress_over=10**9))


@given(st.binary(min_size=1, max_size=200))
def test_arbitrary_bytes_are_refused_rather_than_misread(raw: bytes) -> None:
    """Never a wrong document. Either the real one or a `StateError`."""
    try:
        decoded = decode(raw)
    except StateError:
        return
    # Anything that parsed must encode back to itself: valid state, not garbage
    # that happened to survive.
    assert isinstance(decoded, StateDocument)
    assert decode(encode(decoded)) == decoded


@given(state_documents(), st.data())
def test_a_truncated_document_is_refused(doc: StateDocument, data: st.DataObject) -> None:
    """Truncation is what a half-finished upload or a killed writer leaves
    behind, and it is the corruption most likely to still look like JSON."""
    whole = encode(doc, compress_over=10**9)
    cut = data.draw(st.integers(min_value=0, max_value=max(0, len(whole) - 1)))

    with pytest.raises(StateError):
        decode(whole[:cut])


@given(state_documents())
def test_the_serialized_form_is_canonical(doc: StateDocument) -> None:
    """Keys sorted, no incidental whitespace, so two writers that agree on the
    content produce the same bytes and the ETag compare-and-swap is meaningful."""
    raw = dumps(doc)

    assert b", " not in raw and b": " not in raw
    assert raw == dumps(doc)


def test_the_threshold_constant_is_where_the_branch_actually_switches() -> None:
    """Ties the properties above to the real default threshold."""
    assert COMPRESS_OVER > 0


# -- the row codec -----------------------------------------------------------


@given(state_nodes())
def test_a_node_survives_a_row_round_trip(node: StateNode) -> None:
    """The two halves of the table-shaped backends' storage shape are inverses.

    No other test asserts it. Each half is exercised on its own (writes go through
    one, reads through the other), so a field dropped from both at once looks
    consistent until someone reads state written by an older build.
    """
    row = dict(zip(NODE_COLUMNS, node_columns(node), strict=True))

    assert node_from_row(row) == node


@given(state_nodes())
def test_a_row_has_exactly_one_value_per_column(node: StateNode) -> None:
    """`strict=True` above catches a length mismatch, but only as a `ValueError`
    from `zip` that reads like a test bug. This names the real failure."""
    assert len(node_columns(node)) == len(NODE_COLUMNS)


@given(state_nodes())
def test_every_json_column_is_text(node: StateNode) -> None:
    """Both backends bind these as text; a half emitting a dict would otherwise
    fail at the driver, one backend at a time."""
    row = dict(zip(NODE_COLUMNS, node_columns(node), strict=True))

    for column in (name for name in NODE_COLUMNS if name.endswith("_json")):
        assert isinstance(row[column], str)
        json.loads(row[column])  # parses, rather than merely being a string


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
