"""The invocation's resolved context: which profile, which project, which state.

Two layers, in the order a command needs them. :func:`load_project` reads
``atlantide.toml`` under the ``--profile`` the root callback recorded, so every
command sees the same overlay without threading it through each signature.
:class:`StateTarget` then answers "where does this command's state live" — an
explicit ``--state`` beats the ``[state]`` table, which beats the local default,
and the keyfile paths follow whichever won.

Both exist to be resolved once. A command needs that answer for several purposes
at once — to open the backend, to build the secrets registry, and to say out loud
what it is about to touch — and those must not be able to disagree; resolving
once is also what keeps the "your ``--state`` overrides the remote backend"
warning to one line per command rather than one per lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.project import ProjectConfig
from atlantide.cli.project import load_project as _read_project
from atlantide.core import AtlantideError
from atlantide.secrets import KeyfileValueStore, SecretsRegistry, make_secrets_registry
from atlantide.state import SqliteStateBackend, make_state_backend
from atlantide.state.backend import StateBackend
from atlantide.state.factory import describe

#: State database used when neither ``--state`` nor ``atlantide.toml`` names one.
DEFAULT_STATE = Path("atlantide.db")

#: The ``--profile`` overlay for this invocation, set once by the root callback.
_profile: str | None = None


def use_profile(name: str | None) -> None:
    """Record the ``--profile`` this invocation runs under."""
    global _profile
    _profile = name


def load_project() -> ProjectConfig:
    """The project config for this invocation, under the active ``--profile``."""
    try:
        return _read_project(profile=_profile)
    except AtlantideError as exc:
        fail(str(exc))


@dataclass(frozen=True, slots=True)
class StateTarget:
    """The resolved state destination for one command, and what hangs off it."""

    project: ProjectConfig
    #: The local state file, or ``None`` when state lives in a remote backend.
    local: Path | None

    @classmethod
    def resolve(cls, state: Path | None, project: ProjectConfig) -> StateTarget:
        """Resolve ``--state`` against the project config, warning on an override.

        An explicit ``--state`` file is an explicit choice of local state and
        wins over a remote ``[state]`` table — loudly, so it is never silent.
        """
        if state is None:
            local = None if project.state_backend.is_remote else default_state(project)
        else:
            local = state
            if project.state_backend.is_remote:
                console.print(
                    f"[yellow]warning[/] --state {escape(str(state))} overrides the "
                    f"{project.state_backend.backend!r} backend in atlantide.toml; "
                    f"using the local file"
                )
        return cls(project=project, local=local)

    # -- identity ---------------------------------------------------------

    @property
    def label(self) -> str:
        """``s3://bucket/key (profile prod)`` — this target in one line."""
        where = describe(self.project.state_backend, self.local)
        return f"{where} (profile {self.project.profile})" if self.project.profile else where

    def announce(self) -> None:
        """Say which state is about to be read or written.

        Shared state makes "I am pointed at the wrong environment" both easy (a
        stale shell, a profile not passed) and silent — unexpectedly empty state
        reads exactly like a first run. One line removes the ambiguity.
        """
        console.print(f"[dim]state:[/] {escape(self.label)}")

    # -- what hangs off it ------------------------------------------------

    def open(self) -> StateBackend:
        """The sqlite backend over the local file, or the configured remote one."""
        if self.local is None:
            return make_state_backend(self.project.state_backend, DEFAULT_STATE)
        return SqliteStateBackend(str(self.local))

    def secrets(self) -> SecretsRegistry:
        """The configured secrets registry, plus install key material (per-install
        digest salt + at-rest sealing of sensitive outputs).

        The keyfile is loaded lazily by the material, so a project with no secrets
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
        the project root — the directory ``atlantide.toml`` was read from.
        """
        project = self.project
        base = self.local.parent if self.local is not None else project.directory
        store = (
            project.resolve(project.secrets_store)
            if project.secrets_store
            else base / "atlantide.secrets"
        )
        key = (
            project.resolve(project.secrets_key)
            if project.secrets_key
            else base / "atlantide.key"
        )
        return store, key


def default_state(project: ProjectConfig) -> Path:
    """The project's local state file, whether or not a remote backend is configured."""
    return project.resolve(project.state or DEFAULT_STATE)
