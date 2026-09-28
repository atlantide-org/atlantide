"""How a ``--json`` document reaches stdout, on success and on failure."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from atlantide.cli.console import console
from atlantide.util.errors import also_failed

__all__ = [
    "SCHEMA_VERSION",
    "emit_error_json",
    "emit_json",
    "emit_or_render",
    "error_json",
]

#: Incremented on incompatible payload changes, so a consumer can refuse a
#: document it does not understand.
SCHEMA_VERSION = 1


def emit_json(payload: Mapping[str, Any]) -> None:
    """Write one JSON document to stdout: the whole of this command's output."""
    _write_json({"schema_version": SCHEMA_VERSION, "ok": True, **payload})


def _write_json(doc: dict[str, Any]) -> None:
    """Write ``doc`` to stdout as indented JSON, matching Rich's uncoloured ``print_json``.

    ``console.print_json`` builds a highlighted Rich ``Text`` of the whole
    document, which takes seconds for a plan of a few thousand resources, and can
    put ANSI escapes into machine-read output. The encode/decode/encode round trip
    matches ``print_json`` (``default=str`` first, then a plain re-encode), so key
    coercion and ``default`` handling are identical.
    """
    normalized = json.loads(json.dumps(doc, default=str))
    out = console.file
    out.write(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n")
    out.flush()


def error_json(err: BaseException, *, state: str | None = None) -> dict[str, Any]:
    """The failure envelope, in the same shape a success uses.

    Keeps ``--json`` output parseable when a command fails, not only when it
    succeeds.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {
            "kind": type(err).__name__,
            "message": str(err),
            **{
                key: value
                for key in ("node_id", "op", "resource_type", "line", "col")
                if (value := getattr(err, key, None)) is not None
            },
            "also_failed": [
                {"kind": type(other).__name__, "message": str(other)} for other in also_failed(err)
            ],
        },
    }
    if state is not None:
        payload["state"] = state
    return payload


def emit_error_json(err: BaseException, *, state: str | None = None) -> None:
    _write_json(error_json(err, state=state))


def emit_or_render(
    *,
    json_out: bool,
    payload: Callable[[], Mapping[str, Any]],
    render: Callable[[], None],
    state: str,
) -> None:
    """Emit the JSON document or render the human view.

    Every machine-readable document carries ``state``, since with a shared
    backend a parsed result cannot otherwise say which state it came from.
    """
    if json_out:
        emit_json({**payload(), "state": state})
    else:
        render()
