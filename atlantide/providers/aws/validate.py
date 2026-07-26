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


#: One DNS label: alphanumeric, inner hyphens, 1-63 characters.
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"

#: A dotted name of two or more labels, optionally wildcarded (``*.example.com``,
#: which ACM accepts) and optionally fully qualified with a trailing dot (which
#: Route53 accepts). Two labels are the minimum that makes it a *domain* rather
#: than a bare word — which is the shape a composed ``name_prefix`` produces.
_DOMAIN = re.compile(rf"^(?:\*\.)?{_LABEL}(?:\.{_LABEL})+\.?$")


def domain_name(label: str = "domain name") -> Validator:
    """A dotted DNS name, e.g. ``example.com``, ``*.example.com``, ``example.com.``.

    Worth checking rather than leaving to AWS, because the value can arrive
    without anyone having typed it: a resource whose name field is
    ``physical_name`` and omitted under a ``name_prefix`` stack has one composed
    for it (``{prefix}-{name}-{stack}``), and that composition is a perfectly good
    *resource* name and never a domain. Caught at plan, it names the field; left
    to apply, it is an ACM or Route53 error about a name the config does not
    contain.
    """
    pattern = _DOMAIN

    def run(value: str) -> str | None:
        if len(value) > 253:
            return f"{label} {value!r} exceeds the 253-character limit"
        if not pattern.match(value):
            return (
                f"invalid {label} {value!r}: expected a dotted name such as "
                f"'example.com' (a name composed from a stack's name_prefix is not one)"
            )
        return None

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
