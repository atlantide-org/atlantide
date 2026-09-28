"""Declarative selection of a state backend.

:class:`StateConfig` is the parsed ``[state]`` table from ``atlantide.toml``;
:func:`make_state_backend` turns it into the concrete backend. The heavyweight
imports live inside their branches, so a local run never loads the remote
backends' dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atlantide.core.errors import StateError
from atlantide.state.backend import StateBackend
from atlantide.state.leases import DEFAULT_LOCK_POLICY, DEFAULT_SKEW_MARGIN, LockPolicy
from atlantide.state.sql.dsn import dsn_host


class BackendKind(StrEnum):
    """Backend names accepted in ``[state].backend``."""

    LOCAL = "local"
    S3 = "s3"
    POSTGRES = "postgres"


# Plain `str` values: the CLI prints these (as a --help default and via repr() in
# diagnostics), where an enum member renders as `<BackendKind.LOCAL: 'local'>`.
LOCAL: str = BackendKind.LOCAL.value
S3: str = BackendKind.S3.value
POSTGRES: str = BackendKind.POSTGRES.value
BACKENDS: tuple[str, ...] = tuple(kind.value for kind in BackendKind)

#: Keys that must be set for a backend to be usable, checked before any API call.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    BackendKind.LOCAL: (),
    BackendKind.S3: ("bucket", "key", "lock_table"),
    BackendKind.POSTGRES: (),  # the dsn may also come from the environment; checked separately
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
    #: Kept out of the repr: it may carry a password.
    dsn: str | None = field(default=None, repr=False)
    schema: str | None = None
    # locking, for every backend
    lock_ttl: float | None = None
    lock_renew_interval: float | None = None
    node_timeout: float | None = None
    #: s3, postgres: how far past a lease's expiry another run may take it over.
    #: Judged by the taker's clock on s3 (tolerated skew between hosts) and by
    #: the server's on postgres (grace for a late renewal).
    lock_skew_margin: float | None = None
    #: s3: DynamoDB table holding the journal heads (commit pointers + fences);
    #: defaults to ``lock_table``.
    journal_table: str | None = None
    #: s3: state writes to different nodes kept in flight at once (capped by the
    #: run's parallelism).
    write_concurrency: int | None = None

    @property
    def is_remote(self) -> bool:
        return self.backend != BackendKind.LOCAL

    def validate(self) -> None:
        """Raise :class:`StateError` naming any missing key, before the first API call."""
        if self.backend not in BACKENDS:
            raise StateError(
                f"unknown [state].backend {self.backend!r} — expected one of {', '.join(BACKENDS)}"
            )
        if missing := [key for key in REQUIRED_KEYS[self.backend] if not getattr(self, key)]:
            raise StateError(
                f'[state].backend = "{self.backend}" requires '
                f"{', '.join(missing)} in atlantide.toml"
            )
        if self.backend == BackendKind.POSTGRES and not self.resolved_dsn():
            raise StateError(
                f'[state].backend = "postgres" requires a dsn in atlantide.toml '
                f"or the {DSN_ENV} environment variable"
            )

    def lock_policy(self) -> LockPolicy:
        """The lease timings for this project, defaults filled in.

        ``renew_interval`` tracks a custom ``lock_ttl`` unless it is set too;
        otherwise shortening only the TTL could leave an interval longer than
        the TTL, which never renews in time.
        """
        ttl = self.lock_ttl if self.lock_ttl is not None else DEFAULT_LOCK_POLICY.ttl
        interval = self.lock_renew_interval if self.lock_renew_interval is not None else ttl / 3
        policy = LockPolicy(
            ttl=ttl,
            renew_interval=interval,
            # Capped at ttl / 3: with a short TTL, the default 30s would refuse
            # every write.
            renew_grace=min(DEFAULT_LOCK_POLICY.renew_grace, ttl / 3),
        )
        policy.validate()
        return policy

    def resolved_dsn(self) -> str | None:
        return self.dsn or os.environ.get(DSN_ENV)

    def require(self, key: str) -> str:
        """Read a key ``validate()`` already proved present, narrowed to ``str``."""
        value = getattr(self, key)
        if not isinstance(value, str):  # pragma: no cover - unreachable after validate()
            raise StateError(f"[state].{key} is required")
        return value


def describe(config: StateConfig, local_path: Path | None) -> str:
    """A short, safe label for where state lives, printed before every mutation.

    Naming the target on every command exposes a wrong target (a stale shell, a
    config read from the wrong directory); a plan against unexpectedly empty
    state otherwise looks like a first run.

    ``local_path`` takes precedence because an explicit ``--state`` overrides the
    configured backend; it is always set for a local backend. A postgres DSN is
    reduced to host and schema because it carries a password and this label is
    printed to terminals and CI logs.
    """
    if local_path is not None:
        return str(local_path)
    if config.backend == BackendKind.S3:
        return f"s3://{config.bucket}/{config.key}"
    if config.backend == BackendKind.POSTGRES:
        return f"postgres://{dsn_host(config.resolved_dsn())}/{config.schema or DEFAULT_SCHEMA}"
    return LOCAL  # pragma: no cover - a local backend always resolves a path


def make_state_backend(config: StateConfig, local_path: Path) -> StateBackend:
    """Build the configured backend; ``local_path`` is the sqlite file for ``local``."""
    config.validate()
    skew_margin = (
        config.lock_skew_margin if config.lock_skew_margin is not None else DEFAULT_SKEW_MARGIN
    )
    if config.backend == BackendKind.S3:
        from atlantide.state.s3 import S3StateBackend

        return S3StateBackend(
            config.require("bucket"),
            config.require("key"),
            lock_table=config.require("lock_table"),
            region=config.region,
            profile=config.profile,
            endpoint_url=config.endpoint,
            kms_key_id=config.kms_key_id,
            lock_skew_margin=skew_margin,
            journal_table=config.journal_table,
            write_concurrency=config.write_concurrency,
        )
    if config.backend == BackendKind.POSTGRES:
        from atlantide.state.sql.postgres import PostgresStateBackend

        dsn = config.resolved_dsn() or ""  # validate() proved it is set
        return PostgresStateBackend(
            dsn, schema=config.schema or DEFAULT_SCHEMA, lock_skew_margin=skew_margin
        )
    from atlantide.state.sql.sqlite import SqliteStateBackend

    return SqliteStateBackend(str(local_path))
