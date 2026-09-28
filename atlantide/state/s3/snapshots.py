"""The snapshot object: conditional reads and compare-and-swap writes."""

from __future__ import annotations

from dataclasses import dataclass

from botocore.exceptions import ClientError

from atlantide.core.errors import StateError
from atlantide.state.codec import StateDocument, decode, encode
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import CAS_CODES, MISSING_CODES
from atlantide.state.s3.journal import new_epoch
from atlantide.util.aws import error_code

__all__ = [
    "EntryVanished",
    "Snapshot",
    "SnapshotMoved",
    "Snapshots",
    "compression",
    "encryption",
]


class SnapshotMoved(Exception):
    """Internal: the object changed between the read and the conditional write."""


class EntryVanished(Exception):
    """Internal: an entry vanished mid-read (compacted): restart the read."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A snapshot and the ETag it was read or written at."""

    doc: StateDocument
    etag: str


def compression(body: bytes) -> dict[str, str]:
    """Label a gzipped body for readers accessing the object out-of-band; the
    stored bytes are self-describing either way."""
    return {"ContentEncoding": "gzip"} if body[:2] == b"\x1f\x8b" else {}


def encryption(kms_key_id: str | None) -> dict[str, str]:
    if kms_key_id:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}
    return {"ServerSideEncryption": "AES256"}


class Snapshots:
    """Reads and writes the snapshot at ``ctx.key``, remembering the last one seen."""

    def __init__(self, ctx: S3Context) -> None:
        self._ctx = ctx
        #: The last snapshot read or written, for conditional (304) re-reads.
        self.last: Snapshot | None = None

    def get(self) -> Snapshot | None:
        """The snapshot, re-validated against the last one seen (304 when unchanged)."""
        ctx = self._ctx
        cached = self.last
        extra = {"IfNoneMatch": cached.etag} if cached is not None else {}
        try:
            response = ctx.clients.s3.get_object(Bucket=ctx.bucket, Key=ctx.key, **extra)
        except ClientError as exc:
            code = error_code(exc)
            if code in ("304", "NotModified") and cached is not None:
                return cached
            if code in MISSING_CODES:
                self.last = None
                return None  # a state nothing was ever written to
            raise ctx.read_error(exc) from exc
        snap = Snapshot(decode(response["Body"].read()), str(response.get("ETag", "")))
        self.last = snap
        return snap

    def etag(self) -> str | None:
        ctx = self._ctx
        try:
            return str(ctx.clients.s3.head_object(Bucket=ctx.bucket, Key=ctx.key).get("ETag", ""))
        except ClientError as exc:
            if error_code(exc) in MISSING_CODES:
                return None
            raise ctx.read_error(exc) from exc

    def ensure(self) -> Snapshot:
        """The snapshot, creating an empty one (minting the epoch) if there is none."""
        for _ in range(limits.CAS_ATTEMPTS):
            snap = self.get()
            if snap is not None:
                return snap
            doc = StateDocument(epoch=new_epoch())
            try:
                return self.put(doc, None)
            except SnapshotMoved:
                continue  # another run created it first: read theirs
        raise StateError(f"could not create state {self._ctx.uri}: it kept changing")

    def put(self, doc: StateDocument, etag: str | None) -> Snapshot:
        """Write the snapshot under ``If-Match: etag`` (``If-None-Match: *`` if none)."""
        ctx = self._ctx
        body = encode(doc)
        precondition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            response = ctx.clients.s3.put_object(
                Bucket=ctx.bucket,
                Key=ctx.key,
                Body=body,
                ContentType="application/json",
                **compression(body),
                **encryption(ctx.kms_key_id),
                **precondition,
            )
        except ClientError as exc:
            if error_code(exc) in CAS_CODES:
                raise SnapshotMoved(str(exc)) from exc
            raise StateError(f"cannot write state {ctx.uri}: {exc}") from exc
        snap = Snapshot(doc, str(response.get("ETag", "")))
        self.last = snap
        return snap
