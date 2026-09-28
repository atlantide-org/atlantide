"""botocore client tuning: timeouts, connection pool, and transport-level retries.

*Connection pool.* botocore defaults to 10 connections per client, below
``DEFAULT_PARALLELISM`` (``min(32, cpu * 4)``); a smaller pool queues concurrent calls
and slows the apply without raising an error.

*Read timeout.* :func:`asyncio.to_thread` cannot stop its thread, so the socket timeout
is the only bound on a call that never answers.

The retries here cover the transport layer: connection resets and adaptive client-side
rate limiting under throttling. They compose with the semantic retries in
:mod:`atlantide.providers.aws.provider`, which handle eventual consistency (e.g. the
HTTP 400 from an IAM role that is not yet assumable).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atlantide import __version__
from atlantide.core.tuning import MIN_POOL
from atlantide.util.aws import client_config

if TYPE_CHECKING:
    from botocore.config import Config

__all__ = ["MIN_POOL", "boto_config"]


def boto_config(*, parallelism: int = MIN_POOL) -> Config:
    """Client config for an apply running ``parallelism`` nodes at a time.

    The user agent makes every call attributable to atlantide in CloudTrail. botocore
    is imported only when this is called (see :func:`atlantide.util.aws.client_config`)
    because plugin discovery imports this module for every CLI command, including
    ones that never build a client.
    """
    return client_config(max_pool=max(MIN_POOL, parallelism), user_agent=f"atlantide/{__version__}")
