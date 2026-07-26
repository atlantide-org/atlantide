"""Canonical sample resource types shared across the test suite.

One class per concept, each under the ``test`` provider so node ids are stable
(``default:test.<Class>:<name>``). Tests import these instead of redefining
near-duplicates. See :mod:`tests.support` for the full harness.
"""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import (
    Resource,
    SecretRef,
    computed,
    immutable,
    mutable,
    secret,
)
from atlantide.core.resource import Nested


class NestedValue(Nested):
    """A structured value inside a resource field, like a real `SgRule`.

    The walkers have to see through a model boundary to find a `Ref` inside it —
    that is what makes a route depend on the gateway it points at. Generated
    trees embed this so the property covers the case a plain dict does not.
    """

    label: str = ""
    value: object = None


class _TestResource(Resource):
    """Base carrying the shared ``test`` provider tag."""

    class Meta:
        provider: ClassVar[str] = "test"


class Box(_TestResource):
    """Immutable identity + mutable knobs + a computed output + a ref field."""

    size: int = immutable()
    label: str = mutable(default="")
    ref: str = mutable(default="")
    out: str = computed()


class Grouped(_TestResource):
    """Holds a set-valued property (IR determinism tests).

    A set has no order, so lowering one has to impose one — otherwise the
    canonical IR, and every hash derived from it, depends on PYTHONHASHSEED.
    """

    members: set[str] = mutable(default_factory=set)


class Bucket(_TestResource):
    """Rich resource: physical name, region, mutable knobs, a secret, a computed arn."""

    bucket_name: str = immutable(physical_name=True)
    region: str = immutable(default="eu-west-1")
    versioning: bool = mutable(default=False)
    tags: dict[str, str] = mutable(default_factory=dict)
    token: str = mutable(default="", sensitive=True)
    arn: str = computed()


class Notifier(_TestResource):
    """Downstream resource that consumes another's output via a Ref."""

    target_arn: str = immutable()
    message: str = mutable(default="hello")


class Widget(_TestResource):
    """Minimal mutable resource (drift / interpreter tests)."""

    size: int = immutable(default=0)
    label: str = mutable(default="")


class Thing(_TestResource):
    """Taggable resource with a computed output (policy tests)."""

    size: int = immutable()
    tags: dict[str, str] = mutable(default_factory=dict)
    out: str = computed()


class Server(_TestResource):
    """Physical-name identity (create-before-destroy / replace tests)."""

    name: str = immutable(physical_name=True)
    zone: str = immutable()


class Tagged(_TestResource):
    """Resource with a tags field (stack-tag merge tests)."""

    size: int = immutable()
    tags: dict[str, str] = mutable(default_factory=dict)


class Vault(_TestResource):
    """Holds a secret input handle (secrets tests)."""

    token: SecretRef | None = secret(default=None)
    label: str = mutable(default="")
