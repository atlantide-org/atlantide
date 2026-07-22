"""Serialization shared by every persistent state backend.

Two encodings live here, and both are canonical — keys sorted, no insignificant
whitespace — so unchanged state serializes to identical bytes.

*Rows*, for the table-shaped stores (sqlite, postgres): :data:`NODE_COLUMNS`
names the columns in order, :func:`node_columns` produces one node's values, and
:func:`node_from_row` reads them back. Both backends use the same column names
and the same JSON-encoded text for the structured fields, so this pair is the
only place that knows a node's storage shape.

*Documents*, for the object stores (S3): :class:`StateDocument` plus
:func:`dumps`/:func:`loads` carry the whole graph as one value, because an object
store has no rows to update in place.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from atlantide.core.errors import StateError
from atlantide.state.backend import StateNode

#: Current blob schema version. Bumped when the on-the-wire shape changes;
#: a document written by a newer atlantide is refused rather than misread.
DOCUMENT_VERSION = 1

# Marshalers for the JSON-encoded node columns: validate on load, compact on write.
JSON_OBJ: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
DEPS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
DIGESTS: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])

_NODES: TypeAdapter[dict[str, StateNode]] = TypeAdapter(dict[str, StateNode])

#: The ``nodes`` columns, in the order :func:`node_columns` yields them. Shared
#: by every table-shaped backend so their schemas cannot drift apart.
NODE_COLUMNS = (
    "id",
    "type",
    "provider",
    "provider_version",
    "input_hash",
    "outputs_json",
    "properties_json",
    "deps_json",
    "prevent_destroy",
    "status",
    "secret_digests_json",
)


class Row(Protocol):
    """A name-addressable database row (``sqlite3.Row``, psycopg ``dict_row``)."""

    def __getitem__(self, column: str, /) -> Any: ...


def node_columns(node: StateNode) -> tuple[Any, ...]:
    """One node's column values, ordered as :data:`NODE_COLUMNS`."""
    return (
        node.id,
        node.type,
        node.provider,
        node.provider_version,
        node.input_hash,
        JSON_OBJ.dump_json(node.outputs).decode(),
        JSON_OBJ.dump_json(node.properties).decode(),
        DEPS.dump_json(node.dependencies).decode(),
        node.prevent_destroy,
        node.status,
        DIGESTS.dump_json(node.secret_digests).decode(),
    )


def node_from_row(row: Row) -> StateNode:
    """Rebuild a node from a row whose JSON columns are text."""
    return StateNode(
        id=row["id"],
        type=row["type"],
        provider=row["provider"],
        provider_version=row["provider_version"],
        input_hash=row["input_hash"],
        outputs=JSON_OBJ.validate_json(row["outputs_json"]),
        properties=JSON_OBJ.validate_json(row["properties_json"]),
        dependencies=DEPS.validate_json(row["deps_json"]),
        prevent_destroy=bool(row["prevent_destroy"]),
        status=row["status"],
        secret_digests=DIGESTS.validate_json(row["secret_digests_json"]),
    )


@dataclass(frozen=True, slots=True)
class StateDocument:
    """The whole committed state as one value: nodes, outputs, and the serial."""

    serial: int = 0
    nodes: dict[str, StateNode] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    version: int = DOCUMENT_VERSION


def dumps(doc: StateDocument) -> bytes:
    """Serialize a document to canonical JSON bytes."""
    payload = {
        "version": doc.version,
        "serial": doc.serial,
        "nodes": json.loads(_NODES.dump_json(doc.nodes)),
        "outputs": json.loads(JSON_OBJ.dump_json(doc.outputs)),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


#: A document at or above this many bytes is stored gzipped. An object store
#: rewrites the whole document per node, so bytes written over one apply grow
#: quadratically with the graph; state JSON is repetitive and compresses by
#: roughly an order of magnitude.
COMPRESS_OVER = 64 * 1024

#: gzip's magic number, so a stored document is self-describing and a reader
#: never has to trust a transport header to know how to decode it.
_GZIP_MAGIC = b"\x1f\x8b"


def encode(doc: StateDocument, *, compress_over: int = COMPRESS_OVER) -> bytes:
    """Serialize a document, gzipping it once it is worth the CPU.

    ``mtime=0`` keeps the output a pure function of the input: identical state
    encodes to identical bytes, which is what lets a backend skip a no-op write.
    """
    raw = dumps(doc)
    if len(raw) < compress_over:
        return raw
    return gzip.compress(raw, mtime=0)


def decode(raw: bytes) -> StateDocument:
    """Parse a document written by :func:`encode`, compressed or not."""
    if raw[:2] == _GZIP_MAGIC:
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise StateError(f"corrupt remote state: unreadable gzip stream: {exc}") from exc
    return loads(raw)


def loads(raw: bytes) -> StateDocument:
    """Parse an uncompressed state blob; raise :class:`StateError` if unreadable."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"corrupt remote state: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError("corrupt remote state: expected a JSON object")
    version = payload.get("version")
    if version != DOCUMENT_VERSION:
        raise StateError(
            f"remote state has schema version {version!r}, this build reads "
            f"{DOCUMENT_VERSION} — upgrade atlantide"
        )
    try:
        return StateDocument(
            serial=int(payload["serial"]),
            nodes=_NODES.validate_python(payload.get("nodes", {})),
            outputs=JSON_OBJ.validate_python(payload.get("outputs", {})),
            version=DOCUMENT_VERSION,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise StateError(f"corrupt remote state: {exc}") from exc
