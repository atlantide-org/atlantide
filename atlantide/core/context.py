"""Execution context passed to provider CRUD operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Context:
    """Carried through every provider call during apply/plan.

    ``dry_run`` is set during plan-time provider interactions; ``data`` is an
    extension point (credentials, tracing hooks).
    """

    dry_run: bool = False
    data: dict[str, Any] = field(default_factory=dict)
