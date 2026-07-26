"""One invocation's flags cannot leak into the next.

The CLI needs a handful of answers — ``--debug``, ``--profile``, ``--no-plugins``,
``--audit-log``, ``--json`` — in places a ``typer.Context`` does not reach: inside
``fail``, inside project loading, inside provider discovery. They used to be five
module-level globals in five modules.

The failure that made that worth changing is not hypothetical. A global set by one
command stays set for the next one in the same process, so a ``--json`` run
followed by a plain one printed the plain run's warnings to stderr, where nobody
was looking. ``cli/state.py`` carried an explicit re-set at the top of its
subcommands to work around exactly that.

These tests pin the property that replaced the workaround, rather than the
mechanism: whatever a run sets, the next run does not see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.cli.context import RunContext, begin, current, json_mode, set_json_mode, using


def test_defaults_apply_outside_a_run() -> None:
    """Importing the package must not require a CLI invocation to have happened —
    a library caller reaching `fail()` should not trip over an unset context."""
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
    """The leak that motivated all of this: a `--json` command followed by one
    without it used to keep routing human output to stderr."""
    with using(RunContext()):
        set_json_mode(True)
        assert json_mode() is True

        begin()  # a fresh invocation

        assert json_mode() is False


def test_json_mode_does_not_disturb_the_other_flags() -> None:
    """It is set per command, after the root flags; replacing the whole context
    must carry them forward rather than reset them."""
    with using(RunContext()):
        begin(debug=True, profile="prod")
        set_json_mode(True)

        run = current()
        assert (run.json, run.debug, run.profile) == (True, True, "prod")


def test_a_nested_context_is_restored_on_exit() -> None:
    """What a global could not offer, and the reason this is a ContextVar."""
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
