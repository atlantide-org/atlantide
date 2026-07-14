"""CloudFront resources: an origin access control and a distribution.

Both are global (no ``region`` field) — CloudFront has a single global endpoint.
They are located by their provider-assigned id (``oac_id`` / ``distribution_id``),
restored from state for read/update/delete.
"""

from __future__ import annotations

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws.resources.base import AwsResource

#: AWS-managed "CachingOptimized" cache policy; default for a static site.
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"


class OriginAccessControl(AwsResource):
    """A CloudFront Origin Access Control (OAC) for signing S3 origin requests.

    ``oac_name`` is immutable (a rename replaces it); ``description`` updates in
    place. The origin type and signing behaviour are fixed to the S3 static-site
    shape (``s3`` / ``always`` / ``sigv4``) in the handler.
    """

    oac_name: str = immutable(physical_name=True)
    description: str = mutable(default="")
    oac_id: str = computed()  # OriginAccessControl.Id


class CloudFrontDistribution(AwsResource):
    """A CloudFront distribution fronting a single S3 origin via an OAC.

    ``origin_domain`` (the bucket's regional domain) is immutable; ``oac_id``,
    ``default_root_object``, ``enabled``, ``comment`` and ``tags`` update in place.
    ``domain_name`` is the ``*.cloudfront.net`` URL the site is served from.
    """

    origin_domain: str = immutable()  # {bucket}.s3.{region}.amazonaws.com (a Ref)
    oac_id: str = mutable()  # a Ref to OriginAccessControl.oac_id
    default_root_object: str = mutable(default="index.html")
    enabled: bool = mutable(default=True)
    comment: str = mutable(default="")
    tags: dict[str, str] = mutable(default_factory=dict)
    distribution_id: str = computed()
    domain_name: str = computed()  # <id>.cloudfront.net — the site URL
    arn: str = computed()
