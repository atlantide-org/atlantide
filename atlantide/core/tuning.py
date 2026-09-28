"""Concurrency and client-timeout numbers shared across layers.

``providers``, ``secrets`` and ``state`` each open their own AWS clients and each
need the same bounds, but they are sibling layers that may not import one another
(see the import-linter contracts). Held here so the copies cannot drift apart.

Plain scalars only, with no botocore or asyncio import: each layer builds its own
client object from these, and this module stays free of third-party imports.
"""

from __future__ import annotations

import os
from typing import Literal

#: Nodes reconciled at once by default. An apply is IO-bound on provider APIs, so
#: this scales with the machine but stays capped: past a point the limit is the
#: service's throttling, not the local CPU.
DEFAULT_PARALLELISM = min(32, (os.cpu_count() or 4) * 4)

#: asyncio's own default-executor size (see ``ThreadPoolExecutor``).
_ASYNCIO_DEFAULT_WORKERS = min(32, (os.cpu_count() or 1) + 4)


def io_workers(parallelism: int | None = None) -> int:
    """Threads the event loop's default executor needs for ``parallelism``.

    Blocking SDK calls (boto3) reach the loop through ``asyncio.to_thread``, which
    uses the default executor. asyncio sizes that at ``min(32, cpus + 4)``, below
    :data:`DEFAULT_PARALLELISM` on most machines, so without resizing the thread
    pool, not ``--parallelism``, bounds concurrent provider calls. Never smaller
    than asyncio's own default.
    """
    wanted = parallelism if parallelism is not None else DEFAULT_PARALLELISM
    return max(wanted, _ASYNCIO_DEFAULT_WORKERS)


#: Seconds allowed to establish a connection. Short, so a failed connect reaches
#: the retry layer quickly.
CONNECT_TIMEOUT = 10.0

#: Seconds allowed for one response. Long enough for slow control-plane calls;
#: finite so a hung call cannot stall an apply.
READ_TIMEOUT = 120.0

#: Never size a connection pool below botocore's own default, however low
#: parallelism is.
MIN_POOL = 10

#: Transport-level attempts per call. Kept small: it stacks
#: multiplicatively with the provider's semantic retry loop.
MAX_ATTEMPTS = 3

#: "adaptive" adds client-side rate limiting on top of retries: once the service
#: throttles, every client backs off rather than each discovering the limit
#: independently.
RETRY_MODE: Literal["adaptive"] = "adaptive"
