"""One invocation's flags cannot leak into the next.

The CLI reads ``--debug``, ``--profile``, ``--no-plugins``, ``--audit-log`` and
``--json`` in places a ``typer.Context`` does not reach: inside ``fail``, inside
project loading, inside provider discovery.

A flag set by one command must not stay set for the next one in the same process;
otherwise a ``--json`` run followed by a plain one would print the plain run's
warnings to stderr.

These tests pin that property rather than the mechanism: whatever a run sets, the
next run does not see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.cli.context import RunContext, begin, current, json_mode, set_json_mode, using


def test_defaults_apply_outside_a_run() -> None:
    """Using the package must not require a CLI invocation: a library caller
    reaching `fail()` gets defaults, not an unset context."""
    with using(RunContext()):
        assert current() == RunContext(
            debug=False, profile=None, no_plugins=False, audit_log=None, json=False
        )


def test_begin_records_every_root_flag() -> None:
    with using(RunContext()):
        begin(debug=True, profile="prod", no_plugins=True, audit_log=Path("/tmp/a.jsonl"))

        run = current()
        assert (run.debug, run.profile, run.no_plugins) == (True, "prod", True)
        assert run.audit_log == Path("/tmp/a.jsonl")


def test_begin_clears_json_from_a_previous_run() -> None:
    """Guards against a `--json` command leaving the next command routing human
    output to stderr."""
    with using(RunContext()):
        set_json_mode(enabled=True)
        assert json_mode() is True

        begin()  # a fresh invocation

        assert json_mode() is False


def test_json_mode_does_not_disturb_the_other_flags() -> None:
    """It is set per command, after the root flags; replacing the whole context
    must carry them forward rather than reset them."""
    with using(RunContext()):
        begin(debug=True, profile="prod")
        set_json_mode(enabled=True)

        run = current()
        assert (run.json, run.debug, run.profile) == (True, True, "prod")


def test_a_nested_context_is_restored_on_exit() -> None:
    """The reason the context is a ContextVar rather than a global."""
    with using(RunContext(profile="outer")):
        with using(RunContext(profile="inner")):
            assert current().profile == "inner"
        assert current().profile == "outer"


def test_the_context_is_restored_even_when_the_block_raises() -> None:
    """A command that fails must not leave its flags behind for the next one."""
    with using(RunContext(profile="outer")):
        with pytest.raises(RuntimeError), using(RunContext(profile="inner")):
            raise RuntimeError("boom")
        assert current().profile == "outer"


def test_the_context_is_immutable() -> None:
    """A command mutating a flag mid-run would make it mean two things in one
    stream of output; `set_json_mode` replaces the whole value instead."""
    with pytest.raises(AttributeError):
        current().debug = True  # type: ignore[misc]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
