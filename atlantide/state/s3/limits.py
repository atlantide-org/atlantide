"""Tunables of the S3 backend: service limits, retry budgets, fan-out.

Read as ``limits.NAME`` at the point of use, never imported by name, so a test
that patches one here changes what every collaborator sees.
"""

from __future__ import annotations

#: DynamoDB caps a transaction at 100 items; larger scopes are locked in chunks.
#: Each node costs two items (its lock row, and its head's fence).
TRANSACT_MAX = 100

#: Lock chunks (transactions) of one acquire kept in flight at once.
LOCK_FANOUT = 16

#: ``BatchGetItem`` reads at most 100 keys per call.
BATCH_GET_MAX = 100

#: ``DeleteObjects`` deletes at most 1000 keys per call.
DELETE_MAX = 1000

#: Page size for listing the journal (patched small in tests).
LIST_PAGE = 1000

#: Attempts at a write whose expected head or snapshot kept moving.
CAS_ATTEMPTS = 5

#: Bounded retries for DynamoDB calls that can partially fail transiently.
DDB_ATTEMPTS = 5

#: Jittered exponential backoff between retries of a transient DynamoDB failure
#: (seconds): the first retry waits up to ``BACKOFF_BASE``, each next one twice
#: as long, never more than ``BACKOFF_CAP``.
BACKOFF_BASE = 0.05
BACKOFF_CAP = 1.0

#: A read restarts (from the snapshot) when a fold or bulk write lands under it.
READ_ATTEMPTS = 5

#: A read re-LISTs this many times when a head names an entry the LIST missed.
RELIST_ATTEMPTS = 3

#: Parallel GETs/BatchGets during a read.
READ_FANOUT = 32

#: Commits between background compactions (per backend instance).
COMPACT_EVERY = 1000

#: Default number of writes to different nodes kept in flight.
DEFAULT_WRITE_CONCURRENCY = 16

#: When the fence counter is missing (a recreated lock table), the first fence is
#: minted this far above the highest one the snapshot records, so it cannot
#: collide with a fence a paused run minted after the last fold.
RESEED_GAP = 1_000_000

#: Lease ids an unread row may point at and still be taken, per condition (a
#: DynamoDB ``IN`` list holds at most 100 operands, one is the new lease's).
SHARED_ALLOWED = 98
