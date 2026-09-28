"""Preflight checks: what ``atlantide state check`` reports.

A backend answers :meth:`~atlantide.state.backend.StateBackend.check` with a list
of :class:`Check` results, one per precondition for trusting shared state (the
object exists, versioning is on, the lock table has the right key, conditional
writes are honoured). Reporting them together surfaces every problem at once
rather than one failed API call at a time.

``warn`` flags a risk that does not block a run (versioning off, no lock TTL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: ``ok`` works | ``warn`` works but is risky | ``fail`` broken | ``skip`` not checked.
type Status = Literal["ok", "warn", "fail", "skip"]

OK: Status = "ok"
WARN: Status = "warn"
FAIL: Status = "fail"
SKIP: Status = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    """One preflight result: what was checked, how it went, and what to do."""

    name: str
    status: Status
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Whether this alone should make ``state check`` exit non-zero."""
        return self.status == FAIL
