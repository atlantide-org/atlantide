"""Serialization shared by every persistent state backend.

Every encoding here is canonical (keys sorted, no insignificant whitespace), so
unchanged state serializes to identical bytes.

*Rows*, for the table-shaped stores (sqlite, postgres): :data:`NODE_COLUMNS`
names the columns in order, :func:`node_columns` produces one node's values, and
:func:`node_from_row` reads them back. Both backends use the same column names
and the same JSON-encoded text for the structured fields, so this pair defines a
node's storage shape.

*Snapshots*, for the object store (S3) and for ``state backup`` files:
:class:`StateDocument` plus :func:`dumps`/:func:`loads` carry the whole graph as
one value. On S3 the snapshot is the compacted base of the state journal (see
:mod:`atlantide.state.s3.journal`); a backup file is the same document with the
journal bookkeeping left empty.

*Journal entries*, for the S3 journal: :class:`JournalEntry` plus
:func:`encode_entry`/:func:`decode_entry` carry one node's record (or one stack's
outputs) as written by a single commit.
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from atlantide.core.errors import StateError
from atlantide.state.model import StateNode
from atlantide.util.jsonfmt import compact_json

#: The only snapshot format this build reads and writes. Older formats are
#: refused, not converted; see :func:`loads`.
#:
#: Format 3: ``serial``, ``nodes``, ``outputs``, plus the journal bookkeeping:
#: ``fences`` (raise-only floor per node), ``max_fence``, the watermarks ``wm``
#: (node -> last folded journal seq) and ``owm`` (stack -> same, for outputs),
#: ``gen`` (compaction generation) and ``journal`` (``epoch`` + ``format``).
SNAPSHOT_VERSION = 3

#: Layout version of the journal a snapshot points at, recorded in the snapshot
#: so a reader can refuse an unknown layout.
JOURNAL_FORMAT = 1

#: Version stamped into every journal entry.
ENTRY_VERSION = 1

# Marshalers for the JSON-encoded node columns: validate on load, compact on write.
JSON_OBJ: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
DEPS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
DIGESTS: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])

_NODES: TypeAdapter[dict[str, StateNode]] = TypeAdapter(dict[str, StateNode])
_INTS: TypeAdapter[dict[str, int]] = TypeAdapter(dict[str, int])

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
    "ref_digests_json",
    "depends_on_json",
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
        DIGESTS.dump_json(node.ref_digests).decode(),
        DEPS.dump_json(node.depends_on).decode(),
    )


def node_from_row(row: Row) -> StateNode:
    """Rebuild a node from a row whose JSON columns are text.

    Raises :class:`StateError` naming the row when a JSON column does not decode.
    """
    try:
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
            ref_digests=DIGESTS.validate_json(row["ref_digests_json"]),
            depends_on=DEPS.validate_json(_optional(row, "depends_on_json", "[]")),
        )
    except ValidationError as exc:
        raise StateError(f"corrupt state row {row['id']!r}: {exc}") from exc


def _optional(row: Row, column: str, empty: str) -> Any:
    """``row[column]``, or ``empty`` for a column this row predates (absent or NULL)."""
    try:
        value = row[column]
    except (KeyError, IndexError):  # dict_row / sqlite3.Row without the column
        return empty
    return empty if value is None else value


def outputs_from_text(raw: str | bytes) -> dict[str, Any]:
    """Decode the stored stack-outputs map; :class:`StateError` if it is not a JSON object."""
    try:
        return JSON_OBJ.validate_json(raw)
    except ValidationError as exc:
        raise StateError(f"corrupt state row 'outputs': {exc}") from exc


@dataclass(frozen=True, slots=True)
class StateDocument:
    """The whole committed state as one value: nodes, outputs, and the serial.

    On S3 this is the *snapshot* the journal folds into; the remaining fields are
    that bookkeeping and never move ``serial``:

    * ``fences``: node id -> the highest fence folded from its journal head, a
      raise-only floor kept so the heads can be rebuilt (``state fsck``);
    * ``max_fence``: the highest fence known minted, which re-seeds the fence
      counter if the lock table is recreated;
    * ``wm`` / ``owm``: node id / stack -> the journal seq already folded in
      here (a head at or below it is superseded by this document);
    * ``gen``: how many times a fold has rewritten the snapshot;
    * ``epoch``: names the journal generation (``key.d/<epoch>/``), minted when
      the snapshot is created.

    A ``state backup`` file is the same document with these left empty.
    """

    serial: int = 0
    nodes: dict[str, StateNode] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    version: int = SNAPSHOT_VERSION
    fences: dict[str, int] = field(default_factory=dict)
    max_fence: int = 0
    wm: dict[str, int] = field(default_factory=dict)
    owm: dict[str, int] = field(default_factory=dict)
    gen: int = 0
    epoch: str = ""


def _canonical(payload: Any) -> bytes:
    return compact_json(payload).encode("utf-8")


def dumps(doc: StateDocument) -> bytes:
    """Serialize a document to canonical JSON bytes."""
    return _canonical(
        {
            "version": doc.version,
            "serial": doc.serial,
            "nodes": json.loads(_NODES.dump_json(doc.nodes)),
            "outputs": json.loads(JSON_OBJ.dump_json(doc.outputs)),
            "fences": dict(doc.fences),
            "max_fence": doc.max_fence,
            "wm": dict(doc.wm),
            "owm": dict(doc.owm),
            "gen": doc.gen,
            "journal": {"epoch": doc.epoch, "format": JOURNAL_FORMAT},
        }
    )


#: A document at or above this many bytes is stored gzipped: state JSON
#: compresses by roughly 10x, and a snapshot is rewritten on every fold.
COMPRESS_OVER = 64 * 1024

#: gzip magic number. Stored documents are self-describing, so decoding does not
#: depend on a transport header.
_GZIP_MAGIC = b"\x1f\x8b"


def _compressed(raw: bytes, compress_over: int) -> bytes:
    """Gzip ``raw`` when it reaches ``compress_over`` bytes.

    ``mtime=0`` makes the output a pure function of the input, so identical state
    encodes to identical bytes and a backend can skip a no-op write.
    """
    return raw if len(raw) < compress_over else gzip.compress(raw, mtime=0)


def _decompressed(raw: bytes) -> bytes:
    if raw[:2] != _GZIP_MAGIC:
        return raw
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError, zlib.error) as exc:
        raise StateError(f"corrupt remote state: unreadable gzip stream: {exc}") from exc


def encode(doc: StateDocument, *, compress_over: int = COMPRESS_OVER) -> bytes:
    """Serialize a document, gzipped at or above ``compress_over`` bytes."""
    return _compressed(dumps(doc), compress_over)


def decode(raw: bytes) -> StateDocument:
    """Parse a document written by :func:`encode`, compressed or not."""
    return loads(_decompressed(raw))


def _object(raw: bytes, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"corrupt {what}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError(f"corrupt {what}: expected a JSON object")
    return payload


def loads(raw: bytes) -> StateDocument:
    """Parse an uncompressed snapshot; raise :class:`StateError` if unreadable.

    Only :data:`SNAPSHOT_VERSION` is read. An older format is not converted in
    place; ``state migrate`` (a backend-neutral copy) carries it over.
    """
    payload = _object(raw, "remote state")
    version = payload.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SNAPSHOT_VERSION:
        raise StateError(
            f"state has format version {version}, newer than this build reads "
            f"({SNAPSHOT_VERSION}) — upgrade atlantide"
        )
    if version != SNAPSHOT_VERSION or isinstance(version, bool):
        raise StateError(
            f"state has format version {version!r}, which this atlantide no longer "
            f"reads (it reads only format {SNAPSHOT_VERSION}). Recreate it: with the "
            f"atlantide release that wrote it, `atlantide state migrate --to-local "
            f"old.db`; then with this one, `atlantide state migrate --from old.db "
            f"--force` (or restore from another backend)"
        )
    journal = payload.get("journal", {})
    try:
        if not isinstance(journal, dict):
            raise TypeError("'journal' must be an object")
        if journal.get("format", JOURNAL_FORMAT) != JOURNAL_FORMAT:
            raise StateError(
                f"state journal has format {journal.get('format')!r}, this build "
                f"reads {JOURNAL_FORMAT} — upgrade atlantide"
            )
        return StateDocument(
            serial=_int(payload["serial"]),
            nodes=_NODES.validate_python(payload.get("nodes", {})),
            outputs=JSON_OBJ.validate_python(payload.get("outputs", {})),
            fences=_INTS.validate_python(payload.get("fences", {}), strict=True),
            max_fence=_int(payload.get("max_fence", 0)),
            wm=_INTS.validate_python(payload.get("wm", {}), strict=True),
            owm=_INTS.validate_python(payload.get("owm", {}), strict=True),
            gen=_int(payload.get("gen", 0)),
            epoch=_str(journal.get("epoch", "")),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise StateError(f"corrupt remote state: {exc}") from exc


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {value!r}")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


# -- journal entries -------------------------------------------------------------


class EntryKind(StrEnum):
    """What a journal entry records; also its key segment (``key.d/<epoch>/<kind>/``)."""

    #: One node's record.
    NODE = "log"
    #: One stack's whole output map.
    OUTPUT = "out"


class EntryOp(StrEnum):
    """The two operations an entry can record."""

    PUT = "put"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One committed write, as stored in the S3 journal.

    ``kind`` is :attr:`EntryKind.NODE` (``name`` is a node id and ``record`` its new
    row) or :attr:`EntryKind.OUTPUT` (``name`` is a stack and ``outputs`` its whole
    ``{stack}:{name}`` map). ``op`` is :attr:`EntryOp.DELETE` for a removed node or an
    emptied stack. ``seq`` and ``fence`` are informational: an entry is committed
    only once a DynamoDB head references it.
    """

    kind: str
    name: str
    seq: int
    fence: int
    op: str
    owner: str = ""
    record: StateNode | None = None
    outputs: dict[str, Any] | None = None


_NODE: TypeAdapter[StateNode] = TypeAdapter(StateNode)


def encode_entry(entry: JournalEntry, *, compress_over: int = COMPRESS_OVER) -> bytes:
    """Serialize a journal entry (canonical JSON, gzipped when large)."""
    return _compressed(
        _canonical(
            {
                "v": ENTRY_VERSION,
                "kind": entry.kind,
                "name": entry.name,
                "seq": entry.seq,
                "fence": entry.fence,
                "op": entry.op,
                "owner": entry.owner,
                "record": (
                    json.loads(_NODE.dump_json(entry.record)) if entry.record is not None else None
                ),
                "outputs": (
                    json.loads(JSON_OBJ.dump_json(entry.outputs))
                    if entry.outputs is not None
                    else None
                ),
            }
        ),
        compress_over,
    )


def decode_entry(raw: bytes) -> JournalEntry:
    """Parse a journal entry written by :func:`encode_entry`."""
    payload = _object(_decompressed(raw), "state journal entry")
    if payload.get("v") != ENTRY_VERSION:
        raise StateError(
            f"state journal entry has version {payload.get('v')!r}, this build "
            f"reads {ENTRY_VERSION} — upgrade atlantide"
        )
    try:
        kind, op = payload["kind"], payload["op"]
        if kind not in tuple(EntryKind) or op not in tuple(EntryOp):
            raise ValueError(f"unknown entry kind/op {kind!r}/{op!r}")
        record, outputs = payload.get("record"), payload.get("outputs")
        return JournalEntry(
            kind=kind,
            name=_str(payload["name"]),
            seq=_int(payload["seq"]),
            fence=_int(payload["fence"]),
            op=op,
            owner=_str(payload.get("owner", "")),
            record=_NODE.validate_python(record) if record is not None else None,
            outputs=JSON_OBJ.validate_python(outputs) if outputs is not None else None,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise StateError(f"corrupt state journal entry: {exc}") from exc
