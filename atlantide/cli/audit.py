"""Writing a run's event stream to a file, and to the log.

The record that answers "who changed this, when, and what happened" after the
fact — asked by someone who was not there, about a run nobody thought to watch.

Append-only JSONL, one event per line, so a partial write costs one line rather
than the file, and `tail -f` works while a run is in flight.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from atlantide.cli.errors import fail
from atlantide.core.events import ApplyEvent, EventSink, no_sink
from atlantide.core.logging import get_logger, redact

_log = get_logger("run")


def logging_sink(event: ApplyEvent) -> None:
    """Mirror the stream into the ordinary logger.

    Costs nothing when the level filters it out, and means ``--log-level info``
    shows the run's shape without a separate flag or file.
    """
    _log.info(
        event.phase,
        extra={
            "run_id": event.run_id,
            "node_id": event.node_id,
            "action": event.action,
            **redact(event.detail),
        },
    )


@contextmanager
def audit_file(path: Path | None, *, header: dict[str, Any]) -> Iterator[EventSink]:
    """A sink appending to ``path``, opened for the life of one run.

    ``header`` is written first: who ran this, against which state, with which
    version and config. Without it the events are a list of things that happened
    to nothing in particular — the identity is the part that makes the file an
    audit trail rather than a log.
    """
    if path is None:
        yield no_sink
        return
    try:
        handle = path.open("a", encoding="utf-8")
    except OSError as exc:
        fail(f"cannot open audit log {path}: {exc.strerror or exc}")
    try:
        _write(handle, {"event": "run_header", **redact(header)})

        def emit(event: ApplyEvent) -> None:
            _write(
                handle,
                {
                    "event": event.phase,
                    "run_id": event.run_id,
                    "at": event.at,
                    "node_id": event.node_id,
                    "action": event.action,
                    **redact(event.detail),
                },
            )

        yield emit
    finally:
        handle.close()


def _write(handle: Any, payload: dict[str, Any]) -> None:
    """One line, flushed.

    Flushed per event on purpose: the run this file most needs to describe is
    the one that is about to be killed, and a buffered tail is the part that
    would be missing.
    """
    handle.write(json.dumps(payload, default=str) + "\n")
    handle.flush()
