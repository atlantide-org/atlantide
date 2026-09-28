"""The invocation's resolved context: which profile, which project, which state.

:func:`current_project` reads ``atlantide.toml`` under the ``--profile`` the root
callback recorded, so every command sees the same overlay. :class:`StateTarget`
then resolves where the command's state lives: an explicit ``--state`` beats the
``[state]`` table, which beats the local default, and the keyfile paths follow
whichever wins.

Both are resolved once per command, so opening the backend, building the secrets
registry and announcing the target all use the same answer, and the ``--state``
override warning prints once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from rich.markup import escape

from atlantide.cli.console import out
from atlantide.cli.context import current
from atlantide.cli.errors import fail
from atlantide.cli.project import ProjectConfig, load_project
from atlantide.core import AtlantideError
from atlantide.secrets import KeyfileValueStore, SecretsRegistry, make_secrets_registry
from atlantide.state import LockPolicy, SqliteStateBackend, StateBackend, make_state_backend
from atlantide.state.factory import describe

#: State database used when neither ``--state`` nor ``atlantide.toml`` names one.
DEFAULT_STATE = Path("atlantide.db")


def current_project() -> ProjectConfig:
    """The project config for this invocation, under the active ``--profile``."""
    try:
        return load_project(profile=current().profile)
    except AtlantideError as exc:
        fail(str(exc))


@dataclass(frozen=True, slots=True)
class StateTarget:
    """The resolved state destination for one command, and what hangs off it."""

    project: ProjectConfig
    #: The local state file, or ``None`` when state lives in a remote backend.
    local: Path | None

    @classmethod
    def resolve(cls, state: Path | None, project: ProjectConfig) -> Self:
        """Resolve ``--state`` against the project config.

        An explicit ``--state`` file selects local state and overrides a remote
        ``[state]`` table, with a warning.
        """
        if state is None:
            local = None if project.state_backend.is_remote else default_state(project)
        else:
            local = state
            if project.state_backend.is_remote:
                out().print(
                    f"[yellow]warning[/] --state {escape(str(state))} overrides the "
                    f"{project.state_backend.backend!r} backend in atlantide.toml; "
                    f"using the local file"
                )
        return cls(project=project, local=local)

    # -- identity ---------------------------------------------------------

    @property
    def label(self) -> str:
        """This target in one line, e.g. ``s3://bucket/key (profile prod)``."""
        where = describe(self.project.state_backend, self.local)
        return f"{where} (profile {self.project.profile})" if self.project.profile else where

    def announce(self) -> None:
        """Say which state is about to be read or written.

        Makes a wrong target (stale shell, missing profile) visible; empty state
        otherwise looks like a first run.
        """
        out().print(f"[dim]state:[/] {escape(self.label)}")

    @property
    def lock_policy(self) -> LockPolicy:
        """The lease timings every lock this command takes on its state obeys."""
        return self.project.state_backend.lock_policy()

    # -- what hangs off it ------------------------------------------------

    def open(self) -> StateBackend:
        """The sqlite backend over the local file, or the configured remote one."""
        if self.local is None:
            return make_state_backend(self.project.state_backend, DEFAULT_STATE)
        return SqliteStateBackend(str(self.local))

    def secrets(self) -> SecretsRegistry:
        """The configured secrets registry, plus install key material.

        The key material provides the per-install digest salt and at-rest sealing of
        sensitive outputs. It loads the keyfile lazily, so a project with no secrets
        and no sensitive outputs never creates a key.
        """
        store, key = self._store_and_key()
        return make_secrets_registry(self.project.secrets, store_path=store, key_path=key)

    def value_store(self) -> KeyfileValueStore:
        """The local keyfile value-store behind the ``secret`` subcommands."""
        return KeyfileValueStore(*self._store_and_key())

    def _store_and_key(self) -> tuple[Path, Path]:
        """The value-store and encryption-key paths: toml first, else beside the db.

        With a remote backend there is no local state file, so they fall back to
        the project root (the directory ``atlantide.toml`` was read from).
        """
        project = self.project
        base = self.local.parent if self.local is not None else project.directory
        store = (
            project.resolve(project.secrets_store)
            if project.secrets_store
            else base / "atlantide.secrets"
        )
        key = (
            project.resolve(project.secrets_key) if project.secrets_key else base / "atlantide.key"
        )
        return store, key


def resolve_target(
    state: Path | None, project: ProjectConfig, *, announce: bool = True
) -> StateTarget:
    """This command's state target, announced unless ``announce`` is false.

    Machine-readable output carries the same value as a ``state`` field instead.
    """
    resolved = StateTarget.resolve(state, project)
    if announce:
        resolved.announce()
    return resolved


def default_state(project: ProjectConfig) -> Path:
    """The project's local state file, whether or not a remote backend is configured."""
    return project.resolve(project.state or DEFAULT_STATE)
