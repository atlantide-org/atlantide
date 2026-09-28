"""What every S3 collaborator shares: names, clients, the clock and the one mutex."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

from atlantide.core.errors import StateError
from atlantide.core.logging import get_logger
from atlantide.state.leases import Clock, Lease
from atlantide.state.s3.dynamo import Clients
from atlantide.state.s3.journal import Layout
from atlantide.util.aws import error_code

__all__ = ["S3Context", "backend_log"]

#: The backend's warnings (a deferred compaction, a failed cleanup). The name
#: ``atlantide.state.s3_backend`` is fixed, not derived from the module path,
#: because log filters depend on it.
backend_log = get_logger("state.s3_backend")


@dataclass(slots=True)
class S3Context:
    """One S3 state's configuration and shared runtime.

    ``mutex`` is the backend's only lock. It guards the cached view, the
    commits replayed onto the next view, and the compaction counter; every
    collaborator uses it for that state instead of a lock of its own.
    """

    bucket: str
    key: str
    lock_table: str
    #: DynamoDB table holding the journal heads (defaults to ``lock_table``).
    #: Configured and shown as ``journal_table`` (``[state].journal_table``).
    heads_table: str
    kms_key_id: str | None
    skew_margin: float
    clock: Clock
    clients: Clients
    #: The lease the backend is bound to right now (``StateBackend.bind_lease``).
    bound_lease: Callable[[], Lease | None]
    mutex: threading.Lock = field(default_factory=threading.Lock)

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    @property
    def namespace(self) -> str:
        """What this state's lock rows and heads are scoped to: the state object."""
        return self.uri

    def layout(self, epoch: str) -> Layout:
        return Layout(self.key, self.namespace, epoch)

    def read_error(self, exc: ClientError) -> StateError:
        if error_code(exc) == "NoSuchBucket":
            return StateError(
                f"state bucket {self.bucket!r} does not exist — create it "
                f"(with versioning enabled) before using the s3 state backend"
            )
        return StateError(f"cannot read state {self.uri}: {exc}")

    def lock_error(self, exc: ClientError) -> StateError:
        """A lock-table failure that is infrastructure, not ordinary contention."""
        if error_code(exc) == "ResourceNotFoundException":
            return StateError(
                f"lock table {self.lock_table!r} (or journal table "
                f"{self.heads_table!r}) does not exist — create it with a "
                f"'node_id' (S) hash key before using the s3 state backend"
            )
        return StateError(f"state lock operation failed: {exc}")
