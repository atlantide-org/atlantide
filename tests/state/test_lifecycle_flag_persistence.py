"""A state-only ``prevent_destroy`` write lands in every backend.

The write goes through the run's fenced writer like any other row, so the S3
journal, sqlite and postgres must all record it, and the next ``destroy`` must
read it back. Parametrized over every backend by ``make_backend``.
"""

from __future__ import annotations

import asyncio

from atlantide.core import Lifecycle, PreventDestroyError
from tests.support import Box, FakeProvider, engine_for, globals_of

from .conftest import BackendFactory

GLOBALS = globals_of(Box, Lifecycle=Lifecycle)
A = "default:test.Box:a"
PLAIN = "Box('a', size=1)\n"
PROTECTED = "Box('a', size=1, lifecycle=Lifecycle(prevent_destroy=True))\n"


def test_the_flag_round_trips_through_every_backend(make_backend: BackendFactory) -> None:
    backend = make_backend()
    provider = FakeProvider()
    engine = engine_for(Box, provider=provider, backend=backend)
    asyncio.run(engine.apply(PLAIN, extra_globals=GLOBALS)).unwrap()
    hash_before = backend.load().nodes[A].input_hash
    provider.reset()

    report = asyncio.run(engine.apply(PROTECTED, extra_globals=GLOBALS)).unwrap()

    assert report.state_only == [A]
    assert provider.calls == []
    row = backend.load().nodes[A]
    assert row.prevent_destroy is True
    assert row.input_hash == hash_before
    assert isinstance(asyncio.run(engine.destroy()).failure(), PreventDestroyError)

    asyncio.run(engine.apply(PLAIN, extra_globals=GLOBALS)).unwrap()
    assert backend.load().nodes[A].prevent_destroy is False
    assert asyncio.run(engine.destroy()).unwrap().deleted == [A]
    assert provider.calls == [("delete", "a")]
