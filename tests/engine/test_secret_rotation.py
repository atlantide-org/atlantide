"""A rotated secret re-enters the plan with the field's declared mutability.

The Merkle diff cannot see a rotation — the IR carries handles, not values — so
the secret audit upgrades the NOOP itself. The upgrade must classify like the
diff would: a rotated ``mutable()`` field is an UPDATE, but a rotated
``immutable()`` field cannot be pushed through ``update()`` and has to REPLACE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from atlantide.core import Resource, SecretRef, computed, immutable
from atlantide.engine import Engine
from atlantide.reconcile import Action
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.state import MemoryStateBackend
from tests.support import Bucket, FakeProvider, engine_for, globals_of


class Locked(Resource):
    """A resource whose sensitive field is also immutable."""

    class Meta:
        provider: ClassVar[str] = "test"

    key: str = immutable(sensitive=True)
    out: str = computed()


def _engine(key_path: Path, *classes: type[Resource]) -> Engine:
    secrets = SecretsRegistry(material=KeyMaterial(str(key_path)))
    secrets.register(EnvSecretsProvider(), default=True)
    return engine_for(
        *classes, provider=FakeProvider(), backend=MemoryStateBackend(), secrets=secrets
    )


async def _rotated_change(
    engine: Engine, config: str, extra_globals: dict[str, Any], monkeypatch: Any
) -> Any:
    """Apply at S0=v1, rotate to v2, and return the single planned change."""
    monkeypatch.setenv("S0", "v1")
    (await engine.apply(config, extra_globals=extra_globals)).unwrap()
    monkeypatch.setenv("S0", "v2")
    planned = engine.plan(config, extra_globals=extra_globals).unwrap()
    [change] = planned.changeset.actionable
    return change


async def test_rotating_an_immutable_secret_plans_a_replace(
    tmp_path: Path, monkeypatch: Any
) -> None:
    engine = _engine(tmp_path / "k.key", Locked)
    change = await _rotated_change(
        engine,
        "Locked('l', key=SecretRef('S0'))",
        globals_of(Locked, SecretRef=SecretRef),
        monkeypatch,
    )
    assert change.action is Action.REPLACE
    assert change.changed_fields == ("key",)


async def test_rotating_a_mutable_secret_stays_an_update(tmp_path: Path, monkeypatch: Any) -> None:
    engine = _engine(tmp_path / "k.key", Bucket)
    change = await _rotated_change(
        engine,
        "Bucket('b', bucket_name='b', token=SecretRef('S0'))",
        globals_of(Bucket, SecretRef=SecretRef),
        monkeypatch,
    )
    assert change.action is Action.UPDATE
    assert change.changed_fields == ("token",)
