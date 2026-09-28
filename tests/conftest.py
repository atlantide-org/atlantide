"""Repo-wide pytest configuration.

The shared builders live in :mod:`tests.support`; ``make_engine`` is re-exported
here for suites that import it from this module.

Also registers the Hypothesis profiles. The default profile is **derandomized**:
the same config must always produce the same plan, so the suite must not pass or
fail on an unseeded PRNG. An intermittently failing property gets re-run instead
of investigated.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Verbosity, settings

from tests.support.factories import make_engine

__all__ = ["make_engine"]


@pytest.fixture(autouse=True)
def _cwd_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test from its own ``tmp_path``.

    With no ``atlantide.toml`` the local provider takes the working directory as
    its root and refuses paths outside it, so the files a test writes under
    ``tmp_path`` must be inside the working directory. It also keeps a test that
    resolves a relative path from touching the checkout.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True, scope="session")
def _moto_requests_are_serialized() -> Iterator[None]:
    """Make each mocked AWS request atomic, as it is against the real services.

    Repo-wide rather than in ``tests/state``: every suite that drives the S3
    backend on moto (``tests/state``, ``tests/cli/state/test_remote.py``) needs it,
    and for the rest it is one import and a patched method.

    moto is not thread-safe, and the S3 backend makes S3 and DynamoDB requests
    from several threads (the executor's writers, the background compactor,
    parallel head reads and lock claims), which the real services serve
    correctly. Two races have failed the concurrent tests:

    - Routing. moto's S3 backend reloads ``moto.s3.urls`` (``importlib.reload``)
      for every request it routes. A reload pops the module from
      ``sys.modules`` and puts it back; another thread's reload in between
      fails with "ImportError: module moto.s3.urls not in sys.modules".
    - Handlers. moto mutates live DynamoDB ``Item`` objects without a lock, and
      its read handlers serialize those same objects *after* the backend call
      has returned: ``update_item`` on a missing key stores a key-only item
      and then adds the attributes in place, while a concurrent
      ``batch_get_item`` iterates that item's attributes to size its response;
      ``transact_write_items`` deep-copies whole tables for its rollback.
      Either fails with "dictionary changed size during iteration".

    So the lock wraps moto's whole handling of a request, routing included, for
    every service: locking one service's handler leaves the routing (and every
    other service) racing. It is reentrant and sits below boto3's client
    methods, so the harness can still park a thread just before or after any
    call.
    """
    from moto.core.botocore_stubber import BotocoreStubber

    lock = threading.RLock()
    original = BotocoreStubber.process_request

    def serialized(self: BotocoreStubber, request: Any) -> Any:
        with lock:
            return original(self, request)

    patch = pytest.MonkeyPatch()
    patch.setattr(BotocoreStubber, "process_request", serialized)
    yield
    patch.undo()


#: Shared by every profile. The default 200 ms deadline is measured per example
#: and trips on a loaded CI runner for reasons unrelated to the code under test;
#: a generous explicit budget still catches genuine blowups.
_COMMON = {
    "deadline": 2000,
    "suppress_health_check": [HealthCheck.too_slow],
}

settings.register_profile("dev", max_examples=50, **_COMMON)  # type: ignore[arg-type]
settings.register_profile("ci", max_examples=100, derandomize=True, **_COMMON)  # type: ignore[arg-type]
settings.register_profile(
    "thorough",  # opt-in soak: HYPOTHESIS_PROFILE=thorough uv run pytest
    max_examples=1000,
    verbosity=Verbosity.verbose,
    **_COMMON,  # type: ignore[arg-type]
)
settings.register_profile(
    # The nightly job (`.github/workflows/soak.yml`): `thorough`'s example count
    # without its per-example transcript, which at this scale buries the failure.
    # `print_blob` makes a failure replayable, since the run is not derandomized.
    "soak",
    max_examples=1000,
    print_blob=True,
    **_COMMON,  # type: ignore[arg-type]
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
