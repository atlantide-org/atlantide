"""The S3 state backend: a snapshot + journal in S3, heads and leases in DynamoDB.

Importing this package loads boto3, so :mod:`atlantide.state` does not import it:
the factory imports it only when ``[state].backend = "s3"``.

``backend`` is the façade; ``journal`` (pure layout and folding), ``snapshots``,
``reads``, ``view``, ``writes``, ``outputs``, ``lock_table`` (with
``lease_items``, ``fences`` and ``acquire``) and ``maintenance`` do the work over
one shared ``context``; ``limits`` holds the tunables and ``preflight`` the
``state check`` rows.
"""

from atlantide.state.s3.backend import S3StateBackend
from atlantide.state.s3.maintenance import CompactionReport, FsckReport

__all__ = ["CompactionReport", "FsckReport", "S3StateBackend"]
