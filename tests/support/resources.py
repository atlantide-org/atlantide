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
