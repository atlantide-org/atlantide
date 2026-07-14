"""Shared bases for AWS resources."""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import Resource, immutable, mutable


class AwsResource(Resource):
    """Base for AWS resources; carries the ``aws`` provider tag.

    ``provider_alias`` selects a non-default credential/endpoint profile (see the
    provider's ``aliases`` map) — the multi-account escape hatch. It is immutable:
    moving a resource to another account is a destroy + create, not an in-place
    edit.
    """

    class Meta:
        provider: ClassVar[str] = "aws"

    provider_alias: str | None = immutable(default=None)


class Ec2Resource(AwsResource):
    """Base for EC2 resources: an immutable ``region`` and in-place ``tags``.

    EC2 resources have no name-based ``get``; each is located by its attributes at
    apply time (see ``_Ec2Handler``).
    """

    region: str = immutable()  # required — from the stack's mandatory region
    tags: dict[str, str] = mutable(default_factory=dict)
