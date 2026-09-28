"""Random resources: a value generated once at apply and pinned in state.

The value is produced at apply, persisted, and stable thereafter (re-plan is a
Merkle NOOP). All inputs are immutable, so changing one (such as ``keepers``) is a
REPLACE that regenerates the value.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import model_validator

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

    @model_validator(mode="after")
    def _validate(self) -> Password:
        # An empty password would be generated and pinned without complaint.
        if isinstance(self.length, int) and self.length < 1:
            raise ValueError(f"Password.length must be at least 1, got {self.length}")
        return self


class Id(RandomResource):
    """A random id: ``byte_length`` random bytes, hex-encoded into ``result``."""

    byte_length: int = immutable(default=16)
    result: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> Id:
        # token_hex(0) is "", and a negative length raises only at apply.
        if isinstance(self.byte_length, int) and self.byte_length < 1:
            raise ValueError(f"Id.byte_length must be at least 1, got {self.byte_length}")
        return self


class Timestamp(RandomResource):
    """An RFC-3339 UTC timestamp captured once at apply, pinned in ``result``."""

    result: str = computed()
