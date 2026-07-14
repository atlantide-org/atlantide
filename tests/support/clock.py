"""A manually-advanced clock for deterministic time-dependent tests (lock expiry)."""

from __future__ import annotations


class FakeClock:
    """Manually-advanced clock for deterministic lock-expiry tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
