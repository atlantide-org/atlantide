"""End-to-end: secret refs resolve at apply; value never in source/IR/state."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from atlantide.core import SecretRef
from atlantide.engine import Engine
from atlantide.ir import lower
from atlantide.ir.canonical import to_canonical_json
from atlantide.lang import evaluate_source
from atlantide.secrets import KeyfileValueStore, SecretsRegistry, is_secret_ref_marker
from atlantide.state import MemoryStateBackend
from atlantide.state.sqlite_backend import SqliteStateBackend
from tests.conftest import make_engine
from tests.support import FakeProvider, Vault, globals_of, types_of

TOKEN = "s3cr3t-token-value"
V = "default:test.Vault:v"


TYPES = types_of(Vault)
GLOBALS = globals_of(Vault, SecretRef=SecretRef)
SRC = "Vault('v', token=SecretRef('vault/token'))\n"


def _store(tmp_path: object, value: str = TOKEN) -> tuple[SecretsRegistry, KeyfileValueStore]:
    base = str(tmp_path)  # type: ignore[arg-type]
    store = KeyfileValueStore(os.path.join(base, "s.enc"), os.path.join(base, "s.key"))
    store.set("vault/token", value)
    reg = SecretsRegistry()
    reg.register(store, default=True)
    return reg, store


def _engine(backend: Any, secrets: SecretsRegistry, provider: FakeProvider) -> Engine:
    return make_engine(TYPES, provider, backend=backend, secrets=secrets)


# -- lowering ----------------------------------------------------------------


def test_secret_ref_lowers_to_handle_not_value() -> None:
    registry = evaluate_source(SRC, extra_globals=GLOBALS).unwrap()
    ir = lower(registry)
    node = ir.node(V)
    assert node is not None
    assert is_secret_ref_marker(node.properties["token"])
    assert node.properties["token"] == {"$secret_ref": {"name": "vault/token", "provider": None}}
    # the value is not even known at lowering — only the handle is in the IR bytes
    assert TOKEN.encode() not in to_canonical_json(ir.to_canonical())


def test_secret_ref_is_not_a_dependency_edge() -> None:
    registry = evaluate_source(SRC, extra_globals=GLOBALS).unwrap()
    ir = lower(registry)
    node = ir.node(V)
    assert node is not None
    assert node.dependencies == ()  # a SecretRef is not an upstream Ref


# -- apply / state -----------------------------------------------------------


def test_apply_resolves_ref_and_stores_only_handle_plus_digest(tmp_path: object) -> None:
    backend = MemoryStateBackend()
    secrets, _ = _store(tmp_path)
    provider = FakeProvider()
    engine = _engine(backend, secrets, provider)
    result = asyncio.run(engine.apply(SRC, extra_globals=GLOBALS)).unwrap()

    assert result.created == [V]
    assert provider.seen_values("token") == [TOKEN]  # provider.create got resolved plaintext

    node = backend.load().get(V)
    assert node is not None
    assert is_secret_ref_marker(node.properties["token"])  # handle only
    assert "token" in node.secret_digests  # digest for rotation
    assert TOKEN not in str(node.secret_digests)  # never the value


def test_sqlite_state_file_has_no_value(tmp_path: object) -> None:
    base = str(tmp_path)  # type: ignore[arg-type]
    db = os.path.join(base, "state.db")
    backend = SqliteStateBackend(db)
    secrets, _ = _store(tmp_path)
    asyncio.run(_engine(backend, secrets, FakeProvider()).apply(SRC, extra_globals=GLOBALS))
    backend.close()
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            with open(db + suffix, "rb") as fh:
                blob += fh.read()
    assert TOKEN.encode() not in blob


def test_delete_resolves_ref_for_provider(tmp_path: object) -> None:
    backend = MemoryStateBackend()
    secrets, _ = _store(tmp_path)
    provider = FakeProvider()
    asyncio.run(_engine(backend, secrets, provider).apply(SRC, extra_globals=GLOBALS))
    provider.reset()
    asyncio.run(_engine(backend, secrets, provider).destroy())
    assert provider.seen_values("token") == [TOKEN]  # delete got the re-resolved plaintext


# -- rotation ----------------------------------------------------------------


def test_unchanged_value_is_noop(tmp_path: object) -> None:
    backend = MemoryStateBackend()
    secrets, _ = _store(tmp_path)
    asyncio.run(_engine(backend, secrets, FakeProvider()).apply(SRC, extra_globals=GLOBALS))
    plan = _engine(backend, secrets, FakeProvider()).plan(SRC, extra_globals=GLOBALS).unwrap()
    assert not plan.changeset.actionable  # same value -> nothing to do


def test_rotated_value_upgrades_noop_to_update(tmp_path: object) -> None:
    backend = MemoryStateBackend()
    secrets, store = _store(tmp_path)
    asyncio.run(_engine(backend, secrets, FakeProvider()).apply(SRC, extra_globals=GLOBALS))
    store.set("vault/token", "rotated-value")  # same handle, new value
    plan = _engine(backend, secrets, FakeProvider()).plan(SRC, extra_globals=GLOBALS).unwrap()
    actions = {c.node_id: c.action.value for c in plan.changeset.actionable}
    assert actions == {V: "update"}


# -- plan-time validation ----------------------------------------------------


def test_plan_fails_when_secret_undefined(tmp_path: object) -> None:
    from returns.result import Failure

    base = str(tmp_path)  # type: ignore[arg-type]
    empty = KeyfileValueStore(os.path.join(base, "s.enc"), os.path.join(base, "s.key"))
    secrets = SecretsRegistry()
    secrets.register(empty, default=True)  # nothing set
    engine = _engine(MemoryStateBackend(), secrets, FakeProvider())

    result = engine.plan(SRC, extra_globals=GLOBALS)
    assert isinstance(result, Failure)
    message = str(result.failure())
    assert "undefined secret" in message
    assert "vault/token" in message  # names the missing secret


# -- deploy resolves from the target store (no guard) ------------------------


def test_deploy_resolves_secret_from_store(tmp_path: object) -> None:
    backend = MemoryStateBackend()
    secrets, _ = _store(tmp_path)
    provider = FakeProvider()
    engine = _engine(backend, secrets, provider)
    artifact = engine.build(SRC, extra_globals=GLOBALS).unwrap()
    assert TOKEN.encode() not in artifact.dumps().encode()  # no value in artifact

    result = asyncio.run(engine.deploy(artifact)).unwrap()
    assert result.created == [V]
    assert provider.seen_values("token") == [TOKEN]  # resolved from the deploy env's store
