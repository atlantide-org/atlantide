"""Declarative selection of a state backend.

:class:`StateConfig` is the parsed ``[state]`` table from ``atlantide.toml``;
:func:`make_state_backend` turns it into the concrete backend. The heavyweight
imports live inside their branches, so a local run never loads the remote
backends' dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from atlantide.core.errors import StateError
from atlantide.state.backend import StateBackend

#: Backend names accepted in ``[state].backend``.
LOCAL = "local"
S3 = "s3"
POSTGRES = "postgres"
BACKENDS = (LOCAL, S3, POSTGRES)

#: Keys that must be set for a backend to be usable, checked before any API call.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    LOCAL: (),
    S3: ("bucket", "key", "lock_table"),
    POSTGRES: (),  # the dsn may also come from the environment; checked separately
}

#: Read when ``[state].dsn`` is absent, so credentials stay out of the repo.
DSN_ENV = "ATLANTIDE_STATE_DSN"

#: Postgres schema holding the tables when ``[state].schema`` is unset.
DEFAULT_SCHEMA = "atlantide"


@dataclass(frozen=True)
class StateConfig:
    """The ``[state]`` table. Defaults to the local sqlite file."""

    backend: str = LOCAL
    # s3
    bucket: str | None = None
    key: str | None = None
    lock_table: str | None = None
    kms_key_id: str | None = None
    region: str | None = None
    profile: str | None = None
    endpoint: str | None = None
    # postgres
    dsn: str | None = None
    schema: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.backend != LOCAL

    def validate(self) -> None:
        """Fail fast, naming the missing key, rather than at the first API call."""
        if self.backend not in BACKENDS:
            raise StateError(
                f"unknown [state].backend {self.backend!r} — expected one of "
                f"{', '.join(BACKENDS)}"
            )
        if missing := [key for key in REQUIRED_KEYS[self.backend] if not getattr(self, key)]:
            raise StateError(
                f'[state].backend = "{self.backend}" requires '
                f"{', '.join(missing)} in atlantide.toml"
            )
        if self.backend == POSTGRES and not self.resolved_dsn():
            raise StateError(
                f'[state].backend = "postgres" requires a dsn in atlantide.toml '
                f"or the {DSN_ENV} environment variable"
            )

    def resolved_dsn(self) -> str | None:
        return self.dsn or os.environ.get(DSN_ENV)

    def require(self, key: str) -> str:
        """Read a key ``validate()`` already proved present, narrowed to ``str``."""
        value = getattr(self, key)
        if not isinstance(value, str):  # pragma: no cover - unreachable after validate()
            raise StateError(f"[state].{key} is required")
        return value


def describe(config: StateConfig, local_path: Path | None) -> str:
    """A short, safe label for where state lives — printed before every mutation.

    Targeting the wrong state is the expensive mistake this feature makes
    possible (a stale shell, a config read from the wrong directory), and it is
    silent: a plan against unexpectedly-empty state just looks like a first run.
    Naming the target on every command is what makes it loud instead.

    ``local_path`` wins when set, because an explicit ``--state`` overrides the
    configured backend — and it is always set for a local backend, which is why
    there is no local branch below. A postgres DSN is reduced to host and schema:
    it carries a password, and this string is printed into terminals and CI logs.
    """
    if local_path is not None:
        return str(local_path)
    if config.backend == S3:
        return f"s3://{config.bucket}/{config.key}"
    if config.backend == POSTGRES:
        return f"postgres://{_dsn_host(config.resolved_dsn())}/{config.schema or DEFAULT_SCHEMA}"
    return LOCAL  # pragma: no cover - a local backend always resolves a path


def _dsn_host(dsn: str | None) -> str:
    """The ``host[:port]`` of a DSN, with any credentials dropped."""
    if not dsn:
        return "?"
    try:
        parsed = urlsplit(dsn)
    except ValueError:  # pragma: no cover - defensive
        return "?"
    host = parsed.hostname or "?"
    return f"{host}:{parsed.port}" if parsed.port is not None else host


def make_state_backend(config: StateConfig, local_path: Path) -> StateBackend:
    """Build the configured backend; ``local_path`` is the sqlite file for ``local``."""
    config.validate()
    if config.backend == S3:
        from atlantide.state.s3_backend import S3StateBackend

        return S3StateBackend(
            config.require("bucket"),
            config.require("key"),
            lock_table=config.require("lock_table"),
            region=config.region,
            profile=config.profile,
            endpoint_url=config.endpoint,
            kms_key_id=config.kms_key_id,
        )
    if config.backend == POSTGRES:
        from atlantide.state.postgres_backend import PostgresStateBackend

        dsn = config.resolved_dsn() or ""  # validate() proved it is set
        return PostgresStateBackend(dsn, schema=config.schema or DEFAULT_SCHEMA)
    from atlantide.state.sqlite_backend import SqliteStateBackend

    return SqliteStateBackend(str(local_path))
