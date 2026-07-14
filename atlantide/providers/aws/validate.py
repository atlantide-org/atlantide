"""Composable, resource-agnostic input validators.

A :data:`Validator` maps a string to an error message, or ``None`` when valid.
Compose primitives with :func:`all_of` and call :func:`check` from a resource's
pydantic ``model_validator``, so a bad value is reported during ``plan`` instead
of mid-``apply``. Fields holding an unresolved ``Ref`` are skipped by
:func:`check` (only ``str`` is validated).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

#: A check on a concrete string value: returns an error message, or None if valid.
Validator = Callable[[str], str | None]


def check(value: object, validator: Validator) -> None:
    """Raise ``ValueError`` if ``validator`` rejects ``value`` (unresolved refs skip)."""
    if isinstance(value, str) and (error := validator(value)):
        raise ValueError(error)


def all_of(*validators: Validator) -> Validator:
    """Run validators in order; the first error wins (short-circuits)."""

    def run(value: str) -> str | None:
        for validator in validators:
            if error := validator(value):
                return error
        return None

    return run


def matches(pattern: re.Pattern[str], label: str, requirement: str) -> Validator:
    """Value must match ``pattern``; ``requirement`` describes the rule for the error."""

    def run(value: str) -> str | None:
        return None if pattern.match(value) else f"invalid {label} {value!r}: {requirement}"

    return run


def length_between(low: int, high: int, label: str) -> Validator:
    def run(value: str) -> str | None:
        if low <= len(value) <= high:
            return None
        return f"{label} {value!r} must be {low}-{high} characters"

    return run


def max_length(limit: int, label: str) -> Validator:
    def run(value: str) -> str | None:
        if len(value) <= limit:
            return None
        return f"{label} {value!r} exceeds the {limit}-character limit"

    return run


def forbids(substring: str, label: str) -> Validator:
    def run(value: str) -> str | None:
        if substring not in value:
            return None
        return f"invalid {label} {value!r}: must not contain {substring!r}"

    return run


def one_of(options: Iterable[str], label: str) -> Validator:
    allowed = tuple(options)

    def run(value: str) -> str | None:
        if value in allowed:
            return None
        return f"invalid {label} {value!r}: expected one of {', '.join(allowed)}"

    return run


def ipv4_cidr(label: str = "CIDR") -> Validator:
    """An ``A.B.C.D/M`` block with octets 0-255 and a 0-32 prefix."""
    pattern = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}/(?:\d|[12]\d|3[0-2])$")

    def run(value: str) -> str | None:
        if not pattern.match(value):
            return f"invalid {label} {value!r}: expected A.B.C.D/M form"
        address = value.split("/", 1)[0]
        if any(int(octet) > 255 for octet in address.split(".")):
            return f"invalid {label} {value!r}: an octet is greater than 255"
        return None

    return run
