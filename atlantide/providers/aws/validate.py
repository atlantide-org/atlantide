"""Composable, resource-agnostic input validators.

A :data:`Validator` maps a string to an error message, or ``None`` when valid.
Compose primitives with :func:`all_of` and call :func:`check` from a resource's
pydantic ``model_validator``, so a bad value is reported during ``plan`` instead
of mid-``apply``. :func:`check` validates only ``str`` values, so an unresolved
``Ref`` is skipped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

#: A check on a concrete string value: returns an error message, or None if valid.
type Validator = Callable[[str], str | None]


def check(value: object, validator: Validator) -> None:
    """Raise ``ValueError`` if ``validator`` rejects ``value`` (unresolved refs skip)."""
    if isinstance(value, str) and (error := validator(value)):
        raise ValueError(error)


def all_of(*validators: Validator) -> Validator:
    """Run validators in order and return the first error."""

    def run(value: str) -> str | None:
        for validator in validators:
            if error := validator(value):
                return error
        return None

    return run


def matches(pattern: re.Pattern[str], label: str, requirement: str) -> Validator:
    """The whole value must match ``pattern``; ``requirement`` describes the rule.

    Uses ``fullmatch``: with ``match``, a ``$`` anchor also matches before a trailing
    newline, so ``"name\\n"`` would pass.
    """

    def run(value: str) -> str | None:
        return None if pattern.fullmatch(value) else f"invalid {label} {value!r}: {requirement}"

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
#: Route53 accepts). The two-label minimum rejects a bare word, the shape a name
#: composed from ``name_prefix`` has.
_DOMAIN = re.compile(rf"(?:\*\.)?{_LABEL}(?:\.{_LABEL})+\.?")


def domain_name(label: str = "domain name") -> Validator:
    """A dotted DNS name, e.g. ``example.com``, ``*.example.com``, ``example.com.``.

    Checked at plan because the value may be composed: a ``physical_name`` field
    omitted under a ``name_prefix`` stack becomes ``{prefix}-{name}-{stack}``, which is
    not a domain. At apply, the same value fails with an ACM or Route53 error about a
    name absent from the config.
    """
    pattern = _DOMAIN

    def run(value: str) -> str | None:
        if len(value) > 253:
            return f"{label} {value!r} exceeds the 253-character limit"
        if not pattern.fullmatch(value):
            return (
                f"invalid {label} {value!r}: expected a dotted name such as "
                f"'example.com' (a name composed from a stack's name_prefix is not one)"
            )
        return None

    return run


def ipv4_cidr(label: str = "CIDR") -> Validator:
    """An ``A.B.C.D/M`` block with octets 0-255 and a 0-32 prefix.

    ASCII digits only: ``\\d`` also matches other scripts' digits, which ``int``
    accepts but AWS does not.
    """
    pattern = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}/(?:[0-9]|[12][0-9]|3[0-2])")

    def run(value: str) -> str | None:
        if not pattern.fullmatch(value):
            return f"invalid {label} {value!r}: expected A.B.C.D/M form"
        address = value.split("/", 1)[0]
        if any(int(octet) > 255 for octet in address.split(".")):
            return f"invalid {label} {value!r}: an octet is greater than 255"
        return None

    return run
