"""botocore client tuning: timeouts, connection pool, and transport-level retries.

Every default here exists because the default botocore value is wrong for an
apply. The two that matter most:

*Connection pool.* botocore defaults to 10 connections per client, while
``DEFAULT_PARALLELISM`` is ``min(32, cpu * 4)``. On any ordinary machine that
means most of the apply's concurrency queues behind a pool it cannot see, and the
symptom is a slow apply rather than an error.

*Read timeout.* Without one, a call that never answers hangs its worker thread
forever. ``asyncio.timeout`` cannot help — :func:`asyncio.to_thread` has no way to
kill the thread it started — so the socket timeout is the only thing that
actually ends such a call.

The retry settings here are the *transport* layer: connection resets, and
adaptive client-side rate limiting when the service starts throttling. They
compose with (and do not replace) the semantic retries in
:mod:`atlantide.providers.aws.provider`, which exist for eventual consistency —
an IAM role that is not assumable yet is a perfectly good HTTP 400 that no
transport retry should touch.
"""

from __future__ import annotations

from botocore.config import Config

from atlantide import __version__
from atlantide.core.tuning import (
    CONNECT_TIMEOUT,
    MAX_ATTEMPTS,
    MIN_POOL,
    READ_TIMEOUT,
    RETRY_MODE,
)

__all__ = ["CONNECT_TIMEOUT", "MAX_ATTEMPTS", "MIN_POOL", "READ_TIMEOUT", "boto_config"]


def boto_config(*, parallelism: int = MIN_POOL) -> Config:
    """Client config for an apply running ``parallelism`` nodes at a time.

    ``user_agent_extra`` makes every call attributable in CloudTrail, which is
    half an audit trail for free — the other half being who ran atlantide.
    """
    return Config(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        retries={"max_attempts": MAX_ATTEMPTS, "mode": RETRY_MODE},
        max_pool_connections=max(MIN_POOL, parallelism),
        user_agent_extra=f"atlantide/{__version__}",
    )
