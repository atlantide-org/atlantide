"""Sensitive computed outputs are sealed at rest (S2) and digests are per-install (S3)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from atlantide.engine import Engine
from atlantide.providers import random as random_provider
from atlantide.providers.random import RandomProvider
from atlantide.secrets import KeyMaterial, SecretsRegistry, is_sealed_marker
from atlantide.state import MemoryStateBackend
from atlantide.state.sqlite_backend import SqliteStateBackend
from tests.conftest import make_engine

# A fixed-length password whose generated value we can scan for in the raw DB.
SRC = (
    "from atlantide.providers.random import Password\n"
    "from atlantide.core import output\n"
    "p = Password('p', length=20)\n"
    "output('pw', p.result)\n"
)
P = "default:random.Password:p"


def _sealed_engine(backend: Any, tmp_path: Any) -> tuple[Engine, SecretsRegistry]:
    key = os.path.join(str(tmp_path), "atlantide.key")
    secrets = SecretsRegistry(material=KeyMaterial(key))
    providers = random_provider.TYPES
    reg = make_engine(providers, RandomProvider(), backend=backend, secrets=secrets)
    return reg, secrets


def test_sensitive_output_sealed_in_node_state(tmp_path: Any) -> None:
    backend = MemoryStateBackend()
    engine, _ = _sealed_engine(backend, tmp_path)
    report = asyncio.run(engine.apply(SRC)).unwrap()
    generated = report.outputs["default:pw"]

    node = backend.load().get(P)
    assert node is not None
    assert is_sealed_marker(node.outputs["result"])  # sealed at rest
    assert generated not in str(node.outputs)  # value never in the row


def test_no_sealer_leaves_output_plaintext(tmp_path: Any) -> None:
    # Without install material (dev/tests), state is byte-identical to before.
    backend = MemoryStateBackend()
    engine = make_engine(random_provider.TYPES, RandomProvider(), backend=backend)
    report = asyncio.run(engine.apply(SRC)).unwrap()
    node = backend.load().get(P)
    assert node is not None
    assert node.outputs["result"] == report.outputs["default:pw"]  # plaintext, unsealed


def test_generated_password_never_in_sqlite_file(tmp_path: Any) -> None:
    db = os.path.join(str(tmp_path), "state.db")
    backend = SqliteStateBackend(db)
    engine, _ = _sealed_engine(backend, tmp_path)
    generated = asyncio.run(engine.apply(SRC)).unwrap().outputs["default:pw"]
    backend.close()

    blob = b""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            with open(db + suffix, "rb") as fh:
                blob += fh.read()
    assert generated.encode() not in blob  # neither node outputs nor stack outputs
    assert len(generated) == 20  # sanity: a real value was generated


def test_round_trip_replans_as_noop(tmp_path: Any) -> None:
    # Sealing must not disturb the Merkle NOOP: a second plan sees no changes.
    backend = MemoryStateBackend()
    engine, secrets = _sealed_engine(backend, tmp_path)
    asyncio.run(engine.apply(SRC)).unwrap()
    engine2 = make_engine(random_provider.TYPES, RandomProvider(), backend=backend, secrets=secrets)
    plan = engine2.plan(SRC).unwrap()
    assert not plan.changeset.actionable


def test_digest_matches_falls_back_to_legacy_salt(tmp_path: Any) -> None:
    # A digest written before per-install salts (legacy salt) still verifies, so
    # an upgraded install does not see spurious rotations.
    from atlantide.secrets.digest import secret_digest

    legacy_stored = secret_digest("n:f", "hunter2")  # fixed-salt digest, pre-migration
    salted = SecretsRegistry(material=KeyMaterial(os.path.join(str(tmp_path), "k.key")))
    assert salted.digest("n:f", "hunter2") != legacy_stored  # per-install salt differs
    assert salted.digest_matches("n:f", "hunter2", legacy_stored)  # legacy still accepted
    assert salted.digest_matches("n:f", "hunter2", salted.digest("n:f", "hunter2"))
    assert not salted.digest_matches("n:f", "rotated", legacy_stored)  # real change detected
