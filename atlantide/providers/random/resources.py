"""Random resources: a value generated once at apply and pinned in state.

Unlike a non-deterministic function evaluated in config, these are resources: the
value is produced at apply, persisted, and stable thereafter (re-plan is a Merkle
NOOP). All inputs are immutable, so changing one — e.g. ``keepers`` — is a REPLACE
that regenerates the value, visible in the plan.
"""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import Resource, computed, immutable


class RandomResource(Resource):
    """Base for random resources; carries the ``random`` provider tag."""

    class Meta:
        provider: ClassVar[str] = "random"

    #: Arbitrary values that force regeneration (a REPLACE) when they change.
    keepers: dict[str, str] = immutable(default_factory=dict)


class Uuid(RandomResource):
    """A random UUID v4. ``result`` is the generated UUID string."""

    result: str = computed()


class Password(RandomResource):
    """A random password of ``length`` chars. ``result`` is sensitive (sealed/redacted)."""

    length: int = immutable(default=32)
    result: str = computed(sensitive=True)


class Id(RandomResource):
    """A random id: ``byte_length`` random bytes, hex-encoded into ``result``."""

    byte_length: int = immutable(default=16)
    result: str = computed()


class Timestamp(RandomResource):
    """An RFC-3339 UTC timestamp captured once at apply, pinned in ``result``."""

    result: str = computed()
