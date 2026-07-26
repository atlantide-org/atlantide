"""What this invocation was asked for, in one place.

A handful of root flags — ``--debug``, ``--profile``, ``--no-plugins``,
``--audit-log`` — and the per-command ``--json`` are needed far from where they
were parsed: :func:`~atlantide.cli.errors.fail` decides whether to emit an error
envelope, :func:`~atlantide.cli.target.load_project` needs the profile overlay,
and provider discovery needs to know whether plugins are wanted. None of those
sit anywhere a ``typer.Context`` can reach without threading a parameter through
every helper in the package.

They used to be five module-level globals in five modules, each with its own
setter and ``global`` statement. That scatters one concept and leaks between
invocations in-process: ``cli/state.py`` had to re-set the JSON flag at the top of
its subcommands to undo whatever the previous command in the same test session
had left behind.

A :class:`contextvars.ContextVar` is what this actually is — ambient state for one
execution — and it comes with the thing a global does not: :func:`using` restores
the previous value on exit, so a test or an embedded caller cannot be affected by
a run that came before it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunContext:
    """The invocation-wide answers every command shares.

    Frozen: a command changing one of these mid-run would make the same flag mean
    different things at different points of the same output. :func:`set_json_mode`
    replaces the whole context rather than mutating a field.
    """

    #: ``--debug``: print the full traceback and cause chain on error.
    debug: bool = False
    #: ``--profile``: which ``[profile.<name>]`` overlay atlantide.toml runs under.
    profile: str | None = None
    #: ``--no-plugins``: ignore installed provider plugins.
    no_plugins: bool = False
    #: ``--audit-log``: where this run's events are appended, if anywhere.
    audit_log: Path | None = None
    #: ``--json``: stdout is one JSON document, so human output goes to stderr.
    json: bool = False


#: ``None`` until the app callback runs. A shared default instance would be safe
#: — ``RunContext`` is frozen — but an unset sentinel also distinguishes "no run
#: has begun" from "a run with every flag off", which is what a library caller
#: importing this package gets.
_current: ContextVar[RunContext | None] = ContextVar("atlantide_run", default=None)

_DEFAULT = RunContext()


def current() -> RunContext:
    """This invocation's flags, or all-defaults outside a CLI run."""
    return _current.get() or _DEFAULT


def begin(
    *,
    debug: bool = False,
    profile: str | None = None,
    no_plugins: bool = False,
    audit_log: Path | None = None,
) -> None:
    """Record the root flags. Called once, by the app callback.

    Resets ``json`` along with the rest: it belongs to a single command, and
    carrying it over from a previous invocation is exactly the leak this replaced.
    """
    _current.set(
        RunContext(debug=debug, profile=profile, no_plugins=no_plugins, audit_log=audit_log)
    )


def set_json_mode(enabled: bool) -> None:
    """Declare that stdout belongs to a JSON document.

    Per command rather than per run, because only some commands offer ``--json``
    and a subcommand group may deliberately differ from its parent.
    """
    _current.set(replace(current(), json=enabled))


def json_mode() -> bool:
    return current().json


@contextmanager
def using(context: RunContext) -> Iterator[RunContext]:
    """Run a block under ``context``, restoring whatever was set before.

    The reason this is a ContextVar and not a global: a caller embedding the CLI,
    and every test that invokes more than one command, gets its own state back.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
