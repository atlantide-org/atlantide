"""The mocked AWS itself under threads: each request is atomic, as it is on AWS.

Guards the repo-wide ``_moto_requests_are_serialized`` fixture in
``tests/conftest.py``. Without it the concurrent S3 backend tests fail
intermittently inside moto, not in the backend.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

from ..conftest import BUCKET
from .harness import TIMEOUT
from .support import s3_client

#: The module moto's S3 backend reloads to route each request.
_URLS = "moto.s3.urls"

#: How long a parked request waits for the other one. Serialized, the other one
#: cannot get there while this one is parked, so this bounds the test's runtime.
_PARK = 2.0

type Tracer = Callable[[FrameType, str, Any], Any]


def _in_reload_call(frame: FrameType, event: str) -> bool:
    """``importlib.reload(moto.s3.urls)`` is being entered (the module in hand)."""
    if event != "call" or frame.f_code.co_name != "reload":
        return False
    module = frame.f_locals.get("module")
    return frame.f_globals.get("__name__") == "importlib" and module is sys.modules.get(_URLS)


def _in_reload_window(frame: FrameType, event: str) -> bool:
    """A reload of ``moto.s3.urls`` has popped it from ``sys.modules`` and not yet
    put it back (``importlib._bootstrap._exec``)."""
    return (
        event == "line"
        and frame.f_code.co_name == "_exec"
        and frame.f_locals.get("name") == _URLS
        and _URLS not in sys.modules
    )


class _Park:
    """A trace function that parks its thread, once, where ``spot`` holds, until
    ``until`` is set or :data:`_PARK` passes."""

    def __init__(self, spot: Callable[[FrameType, str], bool], until: threading.Event) -> None:
        self.reached = threading.Event()
        self._spot = spot
        self._until = until

    def __call__(self, frame: FrameType, event: str, _: Any) -> Tracer | None:
        if frame.f_globals.get("__name__") not in ("importlib", "importlib._bootstrap"):
            return None
        self._check(frame, event)
        return self._line

    def _line(self, frame: FrameType, event: str, _: Any) -> Tracer:
        self._check(frame, event)
        return self._line

    def _check(self, frame: FrameType, event: str) -> None:
        if not self.reached.is_set() and self._spot(frame, event):
            self.reached.set()
            self._until.wait(_PARK)


def _traced(tracer: _Park, call: Callable[[], Any], name: str, errors: dict[str, Any]) -> None:
    sys.settrace(tracer)
    try:
        call()
    except BaseException as exc:  # pragma: no cover - the race this guards
        errors[name] = exc
    finally:
        sys.settrace(None)


def test_an_s3_request_is_not_routed_while_another_is_mid_route(aws: None) -> None:
    """Request B is about to reload moto's S3 URL module (it holds the module)
    when request A's reload of it pops it from ``sys.modules``; B goes on while
    A is still in that window.

    Unserialized, B's reload finds the module missing and fails with
    ``ImportError: module moto.s3.urls not in sys.modules`` — what the
    sixteen-writer race test hit under load. Serialized, A cannot start routing
    until B is done, and both succeed.
    """
    b_done = threading.Event()
    a_park = _Park(_in_reload_window, until=b_done)
    b_park = _Park(_in_reload_call, until=a_park.reached)
    errors: dict[str, Any] = {}
    # One client, built up front: clients are thread-safe, creating them is not.
    client = s3_client()

    def request_b() -> None:
        try:
            _traced(b_park, lambda: client.head_bucket(Bucket=BUCKET), "b", errors)
        finally:
            b_done.set()

    b = threading.Thread(target=request_b, name="b")
    b.start()
    assert b_park.reached.wait(TIMEOUT), f"request B never reloaded {_URLS}"
    a = threading.Thread(
        target=_traced, args=(a_park, lambda: client.head_bucket(Bucket=BUCKET), "a", errors)
    )
    a.start()
    b.join(TIMEOUT)
    a.join(TIMEOUT)
    assert not a.is_alive() and not b.is_alive()
    assert errors == {}
    assert _URLS in sys.modules
