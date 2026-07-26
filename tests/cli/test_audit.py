"""Structured logging and the audit trail.

The question these exist to answer is asked after something has gone wrong, by
someone who was not there: who changed this, when, and what happened. A report
rendered to a terminal and discarded cannot answer it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from atlantide.core.events import (
    NODE_FINISH,
    NODE_START,
    RUN_FINISH,
    RUN_START,
    ApplyEvent,
    fanout,
)
from atlantide.core.logging import REDACTED, JsonFormatter, RedactingFilter, redact
from tests.support import Cli

cli = Cli()


def _config(tmp_path: Path, *, sensitive: bool = False) -> Path:
    cfg = tmp_path / "config.py"
    if sensitive:
        cfg.write_text(
            "from atlantide.core import output\n"
            "from atlantide.providers.random import Password\n"
            "p = Password('p', length=12)\n"
            "output('secret_value', p.result)\n"
        )
    else:
        cfg.write_text(
            "from atlantide.providers.local import File\n"
            f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
        )
    return cfg


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# -- the audit file -----------------------------------------------------------


def test_an_apply_records_its_events(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    log = tmp_path / "audit.jsonl"

    cli.run("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")

    phases = [event["event"] for event in _events(log)]
    assert phases[0] == "run_header"
    assert RUN_START in phases and RUN_FINISH in phases
    assert NODE_START in phases and NODE_FINISH in phases


def test_the_header_says_who_ran_what_against_which_state(tmp_path: Path) -> None:
    """Events without an identity are a list of things that happened to nothing
    in particular."""
    cfg = _config(tmp_path)
    log = tmp_path / "audit.jsonl"
    cli.ok("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")

    header = _events(log)[0]
    assert header["command"] == "apply"
    assert header["config"] == str(cfg)
    assert "s.db" in header["state"]
    assert header["version"]


def test_every_event_carries_the_run_id(tmp_path: Path) -> None:
    """Two runs appending to one file have to be separable, and the id is also
    the lock owner — so a lease conflict and an audit line name the same thing."""
    cfg = _config(tmp_path)
    log = tmp_path / "audit.jsonl"
    for content in ("one", "two"):  # each run has real work, so each emits events
        cfg.write_text(
            "from atlantide.providers.local import File\n"
            f"File('f', path={str(tmp_path / 'out.txt')!r}, content={content!r})\n"
        )
        cli.ok("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")

    run_ids = {e["run_id"] for e in _events(log) if e["event"] != "run_header"}
    assert len(run_ids) == 2, "each run is distinguishable"
    assert all(run_id for run_id in run_ids)


def test_the_file_is_appended_to_not_replaced(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    log = tmp_path / "audit.jsonl"
    cli.ok("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")
    first = len(_events(log))
    cli.ok("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")
    assert len(_events(log)) > first


def test_a_run_that_changed_nothing_is_still_recorded(tmp_path: Path) -> None:
    """ "Someone ran apply against prod at 03:00 and it was a no-op" is a fact an
    audit trail gets asked for. A trail that omits runs is incomplete in a way
    nobody notices until they are relying on it."""
    cfg = _config(tmp_path)
    log = tmp_path / "audit.jsonl"
    cli.ok("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")
    before = len(_events(log))

    result = cli.run("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")
    assert "nothing to apply" in result.output

    events = _events(log)
    assert len(events) == before + 1
    assert events[-1]["event"] == "run_header"
    assert events[-1]["planned"] == 0


def test_a_failed_run_still_closes_its_record(tmp_path: Path) -> None:
    """An audit trail that only logs successes is not one."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cfg = tmp_path / "bad.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(blocker / 'child.txt')!r}, content='hi')\n"
    )
    log = tmp_path / "audit.jsonl"

    result = cli.run("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")
    assert result.exit_code == 1

    phases = [event["event"] for event in _events(log)]
    assert "node_fail" in phases
    assert RUN_FINISH in phases, "the run closed its own record despite failing"


def test_no_audit_flag_means_no_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cli.ok("apply", cfg, "--state", tmp_path / "s.db", "-y")
    assert not list(tmp_path.glob("*.jsonl"))


def test_an_unwritable_audit_path_is_a_diagnostic(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run(
        "--audit-log",
        tmp_path / "nope" / "a.jsonl",
        "apply",
        cfg,
        "--state",
        tmp_path / "s.db",
        "-y",
    )
    assert result.exit_code == 1
    assert "cannot open audit log" in result.output


# -- redaction ----------------------------------------------------------------


def test_a_secret_handle_never_reaches_a_record() -> None:
    """Redaction is by construction rather than by discipline at each call site:
    the one place that forgets is the one that matters."""
    assert redact({"$secret_ref": "app/key"}) == REDACTED
    assert redact({"$sealed": "ciphertext"}) == REDACTED


def test_redaction_reaches_nested_values() -> None:
    """A secret is exactly as leaked three dicts down."""
    payload = {"properties": {"token": {"$secret_ref": "app/key"}, "size": 1}}
    assert redact(payload) == {"properties": {"token": REDACTED, "size": 1}}
    assert redact([{"$sealed": "x"}]) == [REDACTED]


def test_the_filter_scrubs_extras_before_formatting() -> None:
    record = logging.LogRecord("atlantide.test", logging.INFO, "", 0, "m", None, None)
    record.__dict__["outputs"] = {"password": {"$sealed": "ciphertext"}}

    RedactingFilter().filter(record)

    assert record.__dict__["outputs"] == {"password": REDACTED}


def test_a_sensitive_output_is_not_written_to_the_audit_file(tmp_path: Path) -> None:
    """End to end: the generated password must not appear in the trail."""
    cfg = _config(tmp_path, sensitive=True)
    log = tmp_path / "audit.jsonl"
    cli.run("--audit-log", log, "apply", cfg, "--state", tmp_path / "s.db", "-y")

    revealed = cli.ok("output", "secret_value", "--state", tmp_path / "s.db", "-r")
    secret = revealed.stdout.strip()
    assert len(secret) == 12
    assert secret not in log.read_text()


# -- the log ------------------------------------------------------------------


def test_the_json_formatter_carries_the_extras() -> None:
    record = logging.LogRecord("atlantide.run", logging.INFO, "", 0, "node_start", None, None)
    record.__dict__["node_id"] = "default:local.File:f"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "node_start"
    assert payload["node_id"] == "default:local.File:f"
    assert payload["level"] == "info"


def test_logs_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    """`--json` promises stdout is one parseable document; a log line in the
    middle of it breaks a consumer on exactly the runs that had something to say.
    """
    cfg = _config(tmp_path)
    result = cli.run("--log-level", "info", "plan", cfg, "--state", tmp_path / "s.db", "--json")
    json.loads(result.stdout)  # parses on its own


def test_an_unknown_log_level_is_refused(tmp_path: Path) -> None:
    result = cli.run("--log-level", "chatty", "providers")
    assert result.exit_code == 1
    assert "--log-level" in result.output


# -- the stream ---------------------------------------------------------------


def test_fanout_survives_a_broken_sink() -> None:
    """An audit file on a full disk is a problem; an apply aborting half-way
    because of one is a bigger problem."""
    seen: list[str] = []

    def broken(_event: ApplyEvent) -> None:
        raise OSError("no space left on device")

    fanout(broken, lambda e: seen.append(e.phase))(ApplyEvent("run", 0.0, RUN_START))

    assert seen == [RUN_START]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
