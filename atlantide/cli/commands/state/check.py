"""``atlantide state check`` — is the backend reachable and safely configured."""

from __future__ import annotations

from contextlib import closing
from typing import Annotated

import typer
from rich.markup import escape

from atlantide.cli.commands.state.common import announced_target
from atlantide.cli.console import console
from atlantide.cli.target import StateTarget
from atlantide.core import AtlantideError
from atlantide.core.check import FAIL, OK, SKIP, WARN, Check, Status

__all__ = ["check"]

#: Rich markup per status, padded to a common width.
_MARK: dict[Status, str] = {
    OK: "[green]ok  [/]",
    WARN: "[yellow]warn[/]",
    FAIL: "[red]fail[/]",
    SKIP: "[dim]--  [/]",
}

ProbeOpt = Annotated[
    bool,
    typer.Option(
        "--probe/--no-probe",
        help="Also verify conditional writes by writing to a scratch key.",
    ),
]


def check(
    probe: ProbeOpt = True,
) -> None:
    """Verify the configured state backend is reachable and safely set up.

    The bucket, the lock table and their settings are the trust root for shared
    state, and atlantide does not create them. This reports every problem at once,
    including settings such as bucket versioning and the lock-table TTL that
    otherwise go unnoticed until they cause a failure.
    """
    target = announced_target()
    with closing(target.open()) as backend:
        checks = backend.check()
        if probe:
            checks.append(backend.probe())
    checks.append(_secrets_check(target))
    for result in checks:
        # Escaped: details quote config keys such as [state].backend.
        console.print(f"{_MARK[result.status]} {result.name}: {escape(result.detail)}")
    if any(result.failed for result in checks):
        raise typer.Exit(1)


def _secrets_check(target: StateTarget) -> Check:
    """Check that the configured secrets provider can serve a secret.

    Building the registry is part of the check: an unknown AWS profile or an
    unreadable keyfile fails there rather than in ``check()``, so both steps
    report failures instead of raising.
    """
    name = target.project.secrets.provider
    try:
        return target.secrets().get(name).check()
    except AtlantideError as exc:
        return Check(f"secrets: {name}", FAIL, str(exc))
    except Exception as exc:
        # Provider SDKs raise their own exception types (botocore ProfileNotFound,
        # OSError on the keyfile); none of them may abort the report.
        return Check(f"secrets: {name}", FAIL, f"{type(exc).__name__}: {exc}")
