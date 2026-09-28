"""Diagnostic logging: levelled, structured, and never on stdout.

Built on the standard ``logging`` module; ``rich`` covers the human-readable path.

Two rules the rest of the codebase relies on:

* **stderr, always.** ``--json`` promises stdout is one parseable document, and a
  log line inside it breaks the consumer.
* **Redacted by construction.** A record's fields pass through
  :class:`RedactingFilter` before formatting, so no call site can write a secret
  handle or a sealed value to a log.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, Literal, override

from atlantide.core._tree import tree_any, tree_map
from atlantide.core.types import SECRET_MARKER_KEYS, SecretRef

#: Everything logs under this root, so one level setting governs the tool and
#: nothing here reconfigures a host application's own logging.
ROOT = "atlantide"

#: Replacement for a redacted value. The key is kept, so the field's presence
#: stays visible.
REDACTED = "(redacted)"

#: Marker keys identifying a value that must never be logged: a secret handle
#: (config/IR) and a sealed value (state).
_SECRET_KEYS = SECRET_MARKER_KEYS

#: The two renderings :func:`configure` installs: human-readable text or one JSON
#: object per line.
type LogFormat = Literal["text", "json"]

#: Attributes `logging` puts on every record; anything else was passed by a
#: caller and belongs in the structured payload.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def get_logger(name: str) -> logging.Logger:
    """The logger for one module, under the atlantide root."""
    return logging.getLogger(f"{ROOT}.{name}")


def _is_secret_marker(value: Any) -> bool:
    """Whether ``value`` is a secret handle or a sealed value.

    Matches the marker dict (IR/state) and the live :class:`SecretRef` (config
    time): serializing a live handle via ``default=str`` would write the secret's
    name into the log.

    Looser than :func:`~atlantide.secrets.material.is_sealed_marker`, which
    requires the marker be the dict's only key: a marker that picked up a sibling
    key is still a secret and must be redacted.
    """
    if isinstance(value, SecretRef):
        return True
    return isinstance(value, dict) and not value.keys().isdisjoint(_SECRET_KEYS)


def _redact_leaf(value: Any) -> Any:
    if _is_secret_marker(value):
        return REDACTED
    # A handle with nested operands (a `Transform`) is redacted whole when any
    # operand is secret: `tree_map` cannot rebuild one, and a secret concatenated
    # into a string is still sensitive.
    if getattr(value, "_atlas_operands", None) is not None:
        return REDACTED if tree_any(value, _is_secret_marker) else value
    return value


def redact(value: Any) -> Any:
    """``value`` with any secret handle or sealed value replaced, at any depth.

    Walks with :func:`~atlantide.core._tree.tree_map`, which descends into
    pydantic models and sets and shares its definition of a child with the
    hashing and lowering paths, so redaction reaches every secret they reach.
    """
    return tree_map(value, _redact_leaf)


class RedactingFilter(logging.Filter):
    """Strips secrets from a record's extra fields and format args before formatting."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD:
                record.__dict__[key] = redact(value)
        args = record.args
        # `log.info("%s", marker)` formats its args into the message. Only an arg
        # holding a secret is rewritten: `redact` lowers tuples, sets, and models,
        # which would change how a harmless arg renders.
        if isinstance(args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in args)
        elif _is_secret_marker(args):
            # `LogRecord` unwraps a lone mapping arg, so `log.info("%s", marker)`
            # arrives with the marker itself as `args`.
            record.args = (REDACTED,)
        elif isinstance(args, Mapping):
            record.args = {key: _redact_arg(value) for key, value in args.items()}
        return True


def _redact_arg(value: Any) -> Any:
    return redact(value) if tree_any(value, _is_secret_marker) else value


class JsonFormatter(logging.Formatter):
    """One JSON object per line: the message plus whatever the caller passed."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        )
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(
    *,
    level: str = "warning",
    fmt: LogFormat = "text",
) -> None:
    """Install the atlantide log handler. Idempotent within a process.

    Defaults to ``warning``; lower ``level`` to see more detail.
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(level.upper())
    # No propagation to the host's root logger: atlantide is importable as a
    # library and must not reconfigure global logging.
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(levelname)-7s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
