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


class RegionalResource(AwsResource):
    """Base for AWS resources that live in one region.

    ``region`` is required and immutable — moving a resource to another region is
    a destroy + create, not an edit. It is rarely written by hand: a ``Stack``
    fills it from its own mandatory ``region`` for every resource in its body that
    declares the field, which is what this base is for.

    Regional is not universal: IAM, CloudFront and Route53 are global and inherit
    :class:`AwsResource` directly, so that the *absence* of a region on those
    types stays a visible fact rather than an inherited field nobody set.
    """

    region: str = immutable()


class TaggedResource(AwsResource):
    """Base for AWS resources that carry tags, in place.

    Separate from :class:`RegionalResource` because the two do not coincide: a
    global ACM certificate is tagged, and an SNS subscription is regional and
    untaggable. Combining them into one base would give half the types a field
    their service rejects.

    Tags update in place on every service that has them, and a ``Stack``'s tags
    are merged into every resource in its body declaring the field.
    """

    tags: dict[str, str] = mutable(default_factory=dict)


class Ec2Resource(RegionalResource, TaggedResource):
    """Base for EC2 resources: an immutable ``region`` and in-place ``tags``.

    EC2 resources have no name-based ``get``; each is located by its attributes at
    apply time (see ``_Ec2Handler``).
    """
