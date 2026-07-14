"""Random provider: generate-once, pin in state, stable on re-apply, drift-free."""

from __future__ import annotations

from atlantide.engine import Engine
from atlantide.providers import random as random_provider
from atlantide.providers.random import RandomProvider
from tests.conftest import make_engine

U = "default:random.Uuid:u"
P = "default:random.Password:p"
ID = "default:random.Id:i"

SRC = (
    "from atlantide.providers.random import Id, Password, Uuid\n"
    "Uuid('u')\n"
    "Password('p', length=12)\n"
    "Id('i', byte_length=8)\n"
)


def _engine() -> Engine:
    return make_engine(random_provider.TYPES, RandomProvider())


async def test_generates_and_pins_values() -> None:
    engine = _engine()
    report = (await engine.apply(SRC)).unwrap()
    assert sorted(report.created) == [ID, P, U]
    state = engine.backend.load()
    assert len(state.get(U).outputs["result"]) == 36  # uuid4 string
    assert len(state.get(P).outputs["result"]) == 12  # password length
    assert len(state.get(ID).outputs["result"]) == 16  # 8 bytes -> 16 hex


async def test_stable_on_reapply() -> None:
    engine = _engine()
    await engine.apply(SRC)
    pinned = engine.backend.load().get(U).outputs["result"]
    report = (await engine.apply(SRC)).unwrap()
    assert set(report.noop) == {U, P, ID} and not report.created  # Merkle NOOP
    assert engine.backend.load().get(U).outputs["result"] == pinned  # value unchanged


async def test_keepers_change_regenerates() -> None:
    engine = _engine()
    await engine.apply(SRC)
    before = engine.backend.load().get(U).outputs["result"]
    rotated = SRC.replace("Uuid('u')", "Uuid('u', keepers={'v': '2'})")
    report = (await engine.apply(rotated)).unwrap()
    assert report.replaced == [U]  # keepers is immutable
    assert engine.backend.load().get(U).outputs["result"] != before  # regenerated


async def test_refresh_reports_no_drift() -> None:
    engine = _engine()
    await engine.apply(SRC)
    report = (await engine.refresh()).unwrap()
    assert not report.has_drift  # nothing external to drift
    assert len(report.in_sync) == 3
