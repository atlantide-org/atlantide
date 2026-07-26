"""Policy value types (pure data).

The policy engine lives in the top-level ``atlantide.policy`` package; only these
serializable value types live here so ``ResourceRegistry`` can collect
config-declared bindings without ``core`` depending on a sibling package.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class PolicyLevel(enum.StrEnum):
    ADVISORY = "advisory"  # violation warns
    MANDATORY = "mandatory"  # violation blocks apply


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    """A policy attached to some resources: name + level + optional type filter.

    ``params`` are the arguments the binding passes to its policy, so a
    parameterised rule is configured where it is attached
    (``enforce("deny-destroy-in-protected", stacks=["prod"])``) rather than by
    the caller who built the registry. Values come from Atlas-lang, so they are
    plain deterministic data, and they round-trip through the ``.atlas``
    artifact with the rest of the binding.
    """

    name: str
    level: PolicyLevel
    types: frozenset[str] | None = None  # None => applies to every resource type
    params: Mapping[str, Any] = field(default_factory=dict)

    def applies_to(self, type_name: str) -> bool:
        return self.types is None or type_name in self.types
