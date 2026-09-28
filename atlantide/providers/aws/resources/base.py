"""Shared bases for AWS resources."""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import Resource, immutable, mutable


class AwsResource(Resource):
    """Base for AWS resources; carries the ``aws`` provider tag.

    ``provider_alias`` selects a non-default credential/endpoint profile (see the
    provider's ``aliases`` map) for multi-account setups. It is immutable: moving a
    resource to another account is a destroy and create.
    """

    class Meta:
        provider: ClassVar[str] = "aws"

    provider_alias: str | None = immutable(default=None)


class RegionalResource(AwsResource):
    """Base for AWS resources that live in one region.

    ``region`` is required and immutable: moving a resource to another region is a
    destroy and create. A ``Stack`` fills it from its own ``region`` for every
    resource in its body that declares the field.

    IAM, CloudFront and Route53 are global and inherit :class:`AwsResource`
    directly, so those types have no ``region`` field.
    """

    region: str = immutable()


class TaggedResource(AwsResource):
    """Base for AWS resources that carry tags, in place.

    Separate from :class:`RegionalResource` because the two do not coincide: a
    global ACM certificate is tagged, and an SNS subscription is regional and
    untaggable.

    Tags update in place on every service that has them, and a ``Stack``'s tags
    are merged into every resource in its body declaring the field.
    """

    tags: dict[str, str] = mutable(default_factory=dict)


class Ec2Resource(RegionalResource, TaggedResource):
    """Base for EC2 resources: an immutable ``region`` and in-place ``tags``.

    EC2 has no name-based ``get``; each resource is located by its id, and a create
    adopts on a node tag (see ``Ec2Handler``).
    """
