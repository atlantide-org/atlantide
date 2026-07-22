"""A plan whose "rotations" are really the wrong keyfile says so.

Rotation digests are salted per install. A teammate who applies against shared
state without the shared ``secrets_key`` recomputes every digest under a
different salt, so every secret reads as rotated and the plan fills with UPDATEs
that would push unchanged values back at the providers. The plan is not so much
wrong as unreadable, and nothing in it points at the keyfile — hence the warning
exercised here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlantide.core import SecretRef
from atlantide.engine import Engine
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import StateBackend
from tests.support import Bucket, FakeProvider, engine_for, globals_of

#: The word the warning must carry for a reader to be able to act on it.
WARNING = "secrets_key"


def _engine(key_path: Path, backend: StateBackend) -> Engine:
    """An engine resolving secrets from the environment, salted by ``key_path``."""
    secrets = SecretsRegistry(material=KeyMaterial(str(key_path)))
    secrets.register(EnvSecretsProvider(), default=True)
    return engine_for(Bucket, provider=FakeProvider(), backend=backend, secrets=secrets)


def _config(count: int) -> str:
    """``count`` buckets whose sensitive ``token`` field holds a secret handle."""
    return "\n".join(
        f"Bucket('b{i}', bucket_name='b{i}', token=SecretRef('S{i}'))" for i in range(count)
    )


def _globals() -> dict[str, Any]:
    return globals_of(Bucket, SecretRef=SecretRef)


async def _apply(engine: Engine, config: str) -> None:
    (await engine.apply(config, extra_globals=_globals())).unwrap()


def _plan(engine: Engine, config: str) -> Any:
    return engine.plan(config, extra_globals=_globals()).unwrap()


def _set_secrets(monkeypatch: Any, count: int) -> None:
    for index in range(count):
        monkeypatch.setenv(f"S{index}", f"value-{index}")


async def test_a_second_keyfile_makes_every_secret_look_rotated(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _set_secrets(monkeypatch, 2)
    backend = MemoryStateBackend()
    config = _config(2)
    await _apply(_engine(tmp_path / "first.key", backend), config)

    # Same state, same secret values, a different install key: every digest misses.
    planned = _plan(_engine(tmp_path / "second.key", backend), config)
    assert any(WARNING in warning for warning in planned.warnings)
    assert "2 resources" in planned.warnings[0]


async def test_the_original_keyfile_plans_clean(tmp_path: Path, monkeypatch: Any) -> None:
    _set_secrets(monkeypatch, 2)
    backend = MemoryStateBackend()
    config = _config(2)
    key = tmp_path / "shared.key"
    await _apply(_engine(key, backend), config)

    planned = _plan(_engine(key, backend), config)
    assert not any(WARNING in warning for warning in planned.warnings)
    assert not planned.changeset.actionable


async def test_one_genuine_rotation_is_not_a_keyfile_warning(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A real rotation stays a plain UPDATE — the tell is that *nothing* matched."""
    _set_secrets(monkeypatch, 2)
    backend = MemoryStateBackend()
    config = _config(2)
    key = tmp_path / "shared.key"
    await _apply(_engine(key, backend), config)

    monkeypatch.setenv("S0", "rotated")
    planned = _plan(_engine(key, backend), config)
    assert not any(WARNING in warning for warning in planned.warnings)
    assert len(planned.changeset.actionable) == 1


async def test_a_single_secret_project_does_not_warn(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """One resource rotating is ordinary; two with none intact is a salt mismatch."""
    _set_secrets(monkeypatch, 1)
    backend = MemoryStateBackend()
    config = _config(1)
    await _apply(_engine(tmp_path / "first.key", backend), config)

    planned = _plan(_engine(tmp_path / "second.key", backend), config)
    assert not any(WARNING in warning for warning in planned.warnings)
