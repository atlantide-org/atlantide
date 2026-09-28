"""botocore helpers shared by every layer that talks to AWS.

``state``, ``secrets`` and ``providers`` are sibling layers that may not import
one another, so the error-shape readers and the client config live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from atlantide.core.tuning import CONNECT_TIMEOUT, MAX_ATTEMPTS, READ_TIMEOUT, RETRY_MODE

if TYPE_CHECKING:
    from botocore.config import Config
    from botocore.exceptions import ClientError

__all__ = ["client_config", "error_code", "error_message"]


def error_code(exc: ClientError) -> str:
    """The AWS error code, e.g. ``NoSuchKey``, or ``""`` when the response carries none."""
    code = exc.response.get("Error", {}).get("Code")
    return str(code) if code is not None else ""


def error_message(exc: ClientError) -> str:
    """The AWS error message, or ``""`` when the response carries none."""
    message = exc.response.get("Error", {}).get("Message")
    return str(message) if message is not None else ""


def client_config(*, max_pool: int | None = None, user_agent: str | None = None) -> Config:
    """A bounded, retried client config.

    The read timeout is required: :func:`asyncio.to_thread` cannot cancel a worker
    thread, so the socket timeout is what ends a hung call. ``max_pool`` and
    ``user_agent`` are passed to botocore only when given.
    """
    # Imported lazily: ``botocore.config`` loads most of botocore (~20ms), which
    # commands that never build a client skip.
    from botocore.config import Config

    options: dict[str, Any] = {}
    if max_pool is not None:
        options["max_pool_connections"] = max_pool
    if user_agent is not None:
        options["user_agent_extra"] = user_agent
    return Config(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        retries={"max_attempts": MAX_ATTEMPTS, "mode": RETRY_MODE},
        **options,
    )
