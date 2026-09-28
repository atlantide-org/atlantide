"""Sinks for a run's event stream: the audit file and the logger.

The audit file records who changed what, when, and with what result. It is
append-only JSONL, one event per line, so a partial write loses one line rather
than the file, and ``tail -f`` works during a run.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict

from atlantide.cli.errors import fail
from atlantide.cli.target import StateTarget
from atlantide.cli.wiring import version
from atlantide.core.events import ApplyEvent, EventSink, no_sink
from atlantide.core.logging import get_logger, redact
from atlantide.engine import Plan

__all__ = ["AuditHeader", "audit_file", "audit_header", "logging_sink"]

_log = get_logger("run")


class AuditHeader(TypedDict):
    """The first line of an audit file, in the order it is written."""

    command: str
    config: str
    state: str
    version: str
    inputs: dict[str, Any]
    envs: list[str]
    planned: int


def audit_header(
    command: str, cfg: Path, state_target: StateTarget, plan_obj: Plan, planned: int
) -> AuditHeader:
    """The header identifying this run in its audit record."""
    return {
        "command": command,
        "config": str(cfg),
        "state": state_target.label,
        "version": version(),
        "inputs": plan_obj.compiled.inputs,
        "envs": list(plan_obj.compiled.envs_selected),
        "planned": planned,
    }


def logging_sink(event: ApplyEvent) -> None:
    """Mirror the event stream into the logger at ``info`` level.

    ``--log-level info`` then shows each run event without an audit file.
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
def audit_file(path: Path | None, *, header: AuditHeader) -> Iterator[EventSink]:
    """A sink appending to ``path``, opened for the life of one run.

    ``header`` (see :func:`audit_header`) is written first: who ran this, against
    which state, with which version and config.
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
    """Write one JSON line and flush it, so a killed run loses no buffered events."""
    handle.write(json.dumps(payload, default=str) + "\n")
    handle.flush()
