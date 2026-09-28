"""A DSN's password must never be echoed: not by a connection failure, not in a label.

The connection tests drive a stand-in driver module, so they run without the
postgres extra.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from atlantide.core.errors import StateError
from atlantide.state.sql.dsn import dsn_host, scrub_dsn
from atlantide.state.sql.postgres import PostgresStateBackend

SECRET = "s3cr3t-pw"


class _Error(Exception):
    pass


class _ProgrammingError(_Error):
    pass


def _failing_to_connect(dsn: str, raised: Exception) -> PostgresStateBackend:
    def connect(*_args: Any, **_kwargs: Any) -> Any:
        raise raised

    backend = object.__new__(PostgresStateBackend)
    backend._psycopg = SimpleNamespace(  # type: ignore[attr-defined]
        Error=_Error, ProgrammingError=_ProgrammingError, connect=connect
    )
    backend._dsn = dsn  # type: ignore[attr-defined]
    backend._row_factory = None  # type: ignore[attr-defined]
    return backend


def _message(backend: PostgresStateBackend) -> StateError:
    with pytest.raises(StateError, match="cannot connect") as info:
        backend._connect()
    return info.value


def test_parse_error_quoting_the_password_is_withheld() -> None:
    dsn = f"host=db password={SECRET} junk"
    err = _message(_failing_to_connect(dsn, _ProgrammingError(f'missing "=" after "{SECRET}"')))
    assert SECRET not in str(err)
    assert err.__cause__ is None and err.__suppress_context__


@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://app:{SECRET}@db:5432/state",
        f"host=db user=app password={SECRET}",
        f"host=db password='{SECRET}' user=app",
    ],
)
def test_operational_error_echoing_the_dsn_is_scrubbed(dsn: str) -> None:
    err = _message(
        _failing_to_connect(dsn, _Error(f"could not connect using {dsn!r}; pw {SECRET}"))
    )
    assert SECRET not in str(err)
    assert "<dsn>" in str(err)


def test_scrub_handles_percent_encoded_url_password() -> None:
    dsn = "postgresql://app:p%40ss-word@db/state"
    assert "p@ss-word" not in scrub_dsn("auth failed for p@ss-word", dsn)
    assert scrub_dsn("no dsn here", "") == "no dsn here"


@pytest.mark.parametrize(
    ("dsn", "host"),
    [
        (None, "?"),
        ("", "?"),
        ("postgresql://user:secret@db.internal:5433/state", "db.internal:5433"),
        ("postgresql://db/state", "db"),
        ("host=db password=secret", "?"),
    ],
)
def test_dsn_host_never_carries_the_credentials(dsn: str | None, host: str) -> None:
    assert dsn_host(dsn) == host
