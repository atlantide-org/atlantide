"""Read-only engine paths never create the install key; persisting runs do.

A plan on a machine without the keyfile used to create a fresh random one: its
salt matched none of the digests in state, so every secret read as rotated, and
the wrong key left on disk later broke unsealing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlantide.core import SecretRef
from atlantide.core.errors import SecretsError
from atlantide.engine import Engine
from atlantide.reconcile import Action
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.state import MemoryStateBackend, StateBackend
from tests.support import Bucket, FakeProvider, Widget, engine_for, globals_of

CONFIG = "Bucket('b', bucket_name='b', token=SecretRef('S0'))"
PLAIN = "Widget('w', size=1)"


def _engine(key: Path, backend: StateBackend) -> Engine:
    secrets = SecretsRegistry(material=KeyMaterial(str(key)))
    secrets.register(EnvSecretsProvider(allow=["S*"]), default=True)
    return engine_for(Bucket, Widget, provider=FakeProvider(), backend=backend, secrets=secrets)


def _globals() -> dict[str, Any]:
    return globals_of(Bucket, Widget, SecretRef=SecretRef)


async def test_plan_apply_plan_and_a_lost_keyfile(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("S0", "hunter2")
    key = tmp_path / "atlantide.key"
    backend = MemoryStateBackend()

    # Fresh state: the plan needs no key and creates none.
    first = _engine(key, backend).plan(CONFIG, extra_globals=_globals()).unwrap()
    assert [c.action for c in first.changeset.actionable] == [Action.CREATE]
    assert not key.exists()

    # The persisting apply creates it, and the digests it writes verify.
    (await _engine(key, backend).apply(CONFIG, extra_globals=_globals())).unwrap()
    assert key.exists()
    assert backend.load().nodes["default:test.Bucket:b"].secret_digests
    again = _engine(key, backend).plan(CONFIG, extra_globals=_globals()).unwrap()
    assert not again.changeset.actionable
    assert not again.warnings

    # Without the keyfile, digests in state fail the plan rather than rotating.
    key.unlink()
    failed = _engine(key, backend).plan(CONFIG, extra_globals=_globals())
    error = failed.failure()
    assert isinstance(error, SecretsError)
    assert str(key) in str(error)
    assert not key.exists()


async def test_an_apply_that_writes_no_digest_creates_no_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Creation is armed, not performed, by a locked run: nothing sealed or
    digested (no sensitive field), no key."""
    key = tmp_path / "atlantide.key"
    backend = MemoryStateBackend()
    (await _engine(key, backend).apply(PLAIN, extra_globals=_globals())).unwrap()
    (await _engine(key, backend).apply(PLAIN, extra_globals=_globals())).unwrap()  # no-op
    assert not key.exists()


async def test_a_no_op_apply_with_digests_in_state_and_no_keyfile_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("S0", "hunter2")
    key = tmp_path / "atlantide.key"
    backend = MemoryStateBackend()
    (await _engine(key, backend).apply(CONFIG, extra_globals=_globals())).unwrap()
    key.unlink()

    result = await _engine(key, backend).apply(CONFIG, extra_globals=_globals())
    assert isinstance(result.failure(), SecretsError)
    assert not key.exists()


def test_build_never_touches_the_key(tmp_path: Path) -> None:
    """An artifact carries secret handles, never digests, so build needs no salt."""
    key = tmp_path / "atlantide.key"
    artifact = _engine(key, MemoryStateBackend()).build(CONFIG, extra_globals=_globals())
    assert artifact.unwrap()
    assert not key.exists()
