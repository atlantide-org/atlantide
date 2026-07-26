"""Concurrency and client-timeout numbers shared across layers.

``providers``, ``secrets`` and ``state`` each open their own AWS clients and each
need the same bounds, but they are sibling layers that may not import one another
(see the import-linter contracts). Held here so the copies cannot drift apart.

Plain scalars only — no botocore, no asyncio. Each layer builds its own client
object from these; ``core`` stays a leaf with no third-party dependency.
"""

from __future__ import annotations

import os
from typing import Literal

#: Nodes reconciled at once by default. An apply is IO-bound on provider APIs, so
#: this scales with the machine but stays capped: past a point the limit is the
#: service's throttling, not the local CPU.
DEFAULT_PARALLELISM = min(32, (os.cpu_count() or 4) * 4)

#: Time allowed to establish a connection. Short: a connect that is going to
#: succeed does so quickly, and failing fast lets the retry layer do its job.
CONNECT_TIMEOUT = 10.0

#: Time allowed for one response. Generous, because some control-plane calls are
#: genuinely slow, but finite — this is the backstop against a hung apply.
READ_TIMEOUT = 120.0

#: Never size a connection pool below botocore's own default, however low
#: parallelism is.
MIN_POOL = 10

#: Transport-level attempts per call. Deliberately small: it stacks
#: multiplicatively with the provider's semantic retry loop.
MAX_ATTEMPTS = 3

#: "adaptive" adds client-side rate limiting on top of retries: once the service
#: throttles, every client backs off rather than each discovering the limit
#: independently.
RETRY_MODE: Literal["adaptive"] = "adaptive"
