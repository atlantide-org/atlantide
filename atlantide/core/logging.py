"""Diagnostic logging: levelled, structured, and never on stdout.

Uses the standard library rather than a logging dependency. The runtime dep set
is six packages, a JSON formatter is forty lines, and ``rich`` already covers the
human-readable path — adding structlog would buy a shorter file and one more
thing to keep compatible.

Two rules the rest of the codebase relies on:

* **stderr, always.** ``--json`` promises stdout is one parseable document, and a
  log line landing in the middle of it breaks a consumer on exactly the runs that
  had something to say.
* **Redacted by construction.** A record's fields pass through
  :class:`RedactingFilter` before formatting, so a secret handle or a sealed value
  cannot reach a log file by someone forgetting to think about it at a call site.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal

from typing_extensions import override

from atlantide.core._tree import tree_any, tree_map
from atlantide.core.types import SECRET_MARKER_KEYS, SecretRef

#: Everything logs under this root, so one level setting governs the tool and
#: nothing here reconfigures a host application's own logging.
ROOT = "atlantide"

#: What a redacted value is replaced with. The key survives, since knowing a
#: field was present is useful and its value is not worth the risk.
REDACTED = "(redacted)"

#: Marker keys identifying a value that must never be logged: a secret handle
#: (config/IR) and a sealed value (state).
_SECRET_KEYS = SECRET_MARKER_KEYS

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

    Both forms: the marker dict (IR/state) *and* the live :class:`SecretRef`
    object (config time). A log call made during config evaluation carries the
    live handle, and serializing it via ``default=str`` would write the secret's
    name into the log file — matching only the dict form waved it through.

    Deliberately looser than :func:`~atlantide.secrets.material.is_sealed_marker`,
    which requires the marker be the dict's only key. Redaction is the wrong place
    to be strict: a marker that picked up a sibling key is still a secret, and the
    stricter predicate would wave it through.
    """
    if isinstance(value, SecretRef):
        return True
    return isinstance(value, dict) and not value.keys().isdisjoint(_SECRET_KEYS)


def _redact_leaf(value: Any) -> Any:
    if _is_secret_marker(value):
        return REDACTED
    # A handle carrying nested values — a `Transform` and its operands — is
    # redacted whole rather than descended: `tree_map` has no branch that rebuilds
    # one, so the choice is to drop it or emit it unexamined. A transform that
    # concatenates a secret into a connection string deserves the same care as the
    # secret itself, and a log is not the place to take the narrower reading.
    if getattr(value, "_atlas_operands", None) is not None:
        return REDACTED if tree_any(value, _is_secret_marker) else value
    return value


def redact(value: Any) -> Any:
    """``value`` with any secret handle or sealed value replaced, at any depth.

    The walk is :func:`~atlantide.core._tree.tree_map` rather than a local one over
    dicts and lists, because "nested" is not only ever a dict. A structured
    resource field is a pydantic model and a set is a container the IR lowers
    later; a walker that stopped at either boundary would log the value it exists
    to hide. ``core._tree`` already answers "what counts as a child" for the
    hashing and lowering paths, and redaction disagreeing with them is precisely
    the gap a secret slips through.
    """
    return tree_map(value, _redact_leaf)


class RedactingFilter(logging.Filter):
    """Strips secrets from a record's extra fields before anything formats it."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD:
                record.__dict__[key] = redact(value)
        return True


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
    fmt: Literal["text", "json"] = "text",
) -> None:
    """Install the atlantide log handler. Idempotent within a process.

    Quiet by default: an operator asked for a plan, not a trace. What the level
    buys is that when something *is* wrong, the detail was never discarded — it
    just was not being printed.
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(level.upper())
    # Never bubble into a host application's root logger: atlantide is importable
    # as a library, and a library must not reconfigure global logging.
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
