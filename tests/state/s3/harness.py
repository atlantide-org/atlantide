"""Deterministic concurrency and crash tooling for the S3 journal backend.

Two wrappers around the boto3 clients an :class:`S3StateBackend` holds:

:class:`SteppingClient`
    Lets a test park a thread before (or after) one chosen call, run something
    else while it is parked, then release it. This makes interleavings such as
    "A stores its entry, B takes over, A commits" deterministic.
:class:`FaultyClient`
    Raises :class:`SimulatedCrash` before or after the *n*-th call of one
    operation — and on every call after that, since a crashed process makes no
    further requests. A ``BaseException``, so nothing in the backend's error
    handling can swallow it, exactly like a ``kill -9``.

Both delegate everything else untouched (``get_paginator`` included), so the
backend under test runs its real code path against moto.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest

from atlantide.state.s3 import S3StateBackend

#: How long a parked or waiting thread waits before the test is declared hung.
TIMEOUT = 10.0


class SimulatedCrash(BaseException):
    """The process died here. Not an ``Exception``: nothing may catch it."""


type Match = Callable[[dict[str, Any]], bool]


@dataclass
class Pause:
    """One parking spot: the matching call waits here until :meth:`release`."""

    method: str
    match: Match
    after: bool = False
    #: Only threads with this name park (``None``: any thread).
    thread: str | None = None
    reached: threading.Event = field(default_factory=threading.Event)
    _go: threading.Event = field(default_factory=threading.Event)
    _used: bool = False

    def wanted(self, method: str, kwargs: dict[str, Any], *, after: bool) -> bool:
        if self._used or method != self.method or after != self.after:
            return False
        if self.thread is not None and threading.current_thread().name != self.thread:
            return False
        return self.match(kwargs)

    def park(self) -> None:
        self._used = True
        self.reached.set()
        if not self._go.wait(TIMEOUT):  # pragma: no cover - a hung test
            raise AssertionError(f"parked {self.method} was never released")

    def wait(self) -> None:
        """Block the test until a thread is parked here."""
        if not self.reached.wait(TIMEOUT):  # pragma: no cover - a hung test
            raise AssertionError(f"no thread reached {self.method}")

    def reached_before(self, thread: threading.Thread) -> bool:
        """Whether a thread parks here before ``thread`` finishes (it may finish
        first when the code under test does not make this call)."""
        while not self.reached.wait(0.01):
            if not thread.is_alive():
                return self.reached.is_set()
        return True

    def release(self) -> None:
        self._go.set()


class SteppingClient:
    """A boto3 client whose chosen calls park until the test lets them go."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._pauses: list[Pause] = []
        self._hooks: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        self._lock = threading.Lock()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def every(self, method: str, hook: Callable[[dict[str, Any]], None]) -> None:
        """Run ``hook(kwargs)`` right after every ``method`` call returns."""
        self._hooks.append((method, hook))

    def pause(
        self,
        method: str,
        match: Match = lambda _: True,
        *,
        after: bool = False,
        thread: str | None = None,
    ) -> Pause:
        spot = Pause(method, match, after=after, thread=thread)
        self._pauses.append(spot)
        return spot

    def _parking(self, method: str, kwargs: dict[str, Any], *, after: bool) -> Pause | None:
        with self._lock:
            for spot in self._pauses:
                if spot.wanted(method, kwargs, after=after):
                    spot._used = True
                    return spot
        return None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_") or name in ("get_paginator", "meta"):
            return attr

        def call(**kwargs: Any) -> Any:
            with self._lock:
                self.calls.append((name, kwargs))
            if (spot := self._parking(name, kwargs, after=False)) is not None:
                spot.park()
            result = attr(**kwargs)
            for method, hook in self._hooks:
                if method == name:
                    hook(kwargs)
            if (spot := self._parking(name, kwargs, after=True)) is not None:
                spot.park()
            return result

        return call


class Fault:
    """A crash planned at the ``n``-th ``method`` call (1-based), shared by every
    client it wraps: one process, one death."""

    def __init__(self, method: str, n: int, *, after: bool = False) -> None:
        self.method = method
        self.n = n
        self.after = after
        self._seen = 0
        self._lock = threading.Lock()
        self.crashed = threading.Event()

    def wrap(self, inner: Any) -> FaultyClient:
        return FaultyClient(inner, self)

    def hit(self, name: str) -> bool:
        """Count a call; whether it is the fatal one. Raises once crashed."""
        if self.crashed.is_set():
            raise SimulatedCrash(f"{name} after the crash")
        with self._lock:
            if name != self.method:
                return False
            self._seen += 1
            return self._seen == self.n

    def die(self, where: str, name: str) -> SimulatedCrash:
        self.crashed.set()
        return SimulatedCrash(f"{where} {name} #{self.n}")


class FaultyClient:
    """A boto3 client that dies where its :class:`Fault` says (and stays dead)."""

    def __init__(self, inner: Any, fault: Fault) -> None:
        self._inner = inner
        self._fault = fault

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_") or name == "meta":
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            fatal = self._fault.hit(name)
            if fatal and not self._fault.after:
                raise self._fault.die("before", name)
            try:
                result = attr(*args, **kwargs)
            except Exception:
                # The call completed with an error response (a refused
                # transaction, say): dying "after" it is still a crash point.
                if fatal:
                    raise self._fault.die("after", name) from None
                raise
            if fatal:
                raise self._fault.die("after", name)
            return result

        return call


def crashing(backend: S3StateBackend, fault: Fault) -> S3StateBackend:
    """``backend`` with both clients dying at ``fault``."""
    backend._s3, backend._ddb = fault.wrap(backend._s3), fault.wrap(backend._ddb)
    return backend


class CountingClient:
    """Records the operation name of every call (to size a crash sweep)."""

    def __init__(self, inner: Any, log: list[str]) -> None:
        self._inner = inner
        self._log = log
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_") or name == "meta":
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self._log.append(name)
            return attr(*args, **kwargs)

        return call


def stepping(backend: S3StateBackend) -> tuple[SteppingClient, SteppingClient]:
    """Swap ``backend``'s clients for stepping ones; returns ``(s3, ddb)``."""
    s3, ddb = SteppingClient(backend._s3), SteppingClient(backend._ddb)
    backend._s3, backend._ddb = s3, ddb
    return s3, ddb


def in_thread(work: Callable[[], Any], name: str) -> tuple[threading.Thread, list[Any]]:
    """Run ``work`` on a named thread; the returned list gets its result or exception."""
    outcome: list[Any] = []

    def target() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:  # handed back to the test
            outcome.append(exc)

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread, outcome


def joined(thread: threading.Thread, outcome: list[Any]) -> Any:
    """Wait for ``thread``; return its result (or re-raise what it raised)."""
    thread.join(TIMEOUT)
    assert not thread.is_alive(), f"{thread.name} hung"
    (result,) = outcome
    if isinstance(result, BaseException):
        raise result
    return result


def share_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """One boto3 client per service for every backend built during the test.

    Tests model each "process" as a fresh backend, and building its clients
    dominates that cost. Sharing them changes nothing observable: a backend's
    caches are its own, and fault wrappers go around the shared client. Clients
    are built on first use, so inside the test's ``mock_aws``.
    """
    real = boto3.Session
    clients: dict[str, Any] = {}

    class Shared:
        def __init__(self, **kwargs: Any) -> None:
            self._session = real(**kwargs)

        def client(self, service: str, **kwargs: Any) -> Any:
            if service not in clients:
                clients[service] = self._session.client(service, **kwargs)
            return clients[service]

    monkeypatch.setattr(boto3, "Session", Shared)
