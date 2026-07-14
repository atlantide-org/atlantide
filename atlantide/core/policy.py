"""Policy value types (pure data).

The policy engine lives in the top-level ``atlantide.policy`` package; only these
serializable value types live here so ``ResourceRegistry`` can collect
config-declared bindings without ``core`` depending on a sibling package.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class PolicyLevel(enum.StrEnum):
    ADVISORY = "advisory"    # violation warns
    MANDATORY = "mandatory"  # violation blocks apply


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    """A policy attached to some resources: name + level + optional type filter."""

    name: str
    level: PolicyLevel
    types: frozenset[str] | None = None  # None => applies to every resource type

    def applies_to(self, type_name: str) -> bool:
        return self.types is None or type_name in self.types
