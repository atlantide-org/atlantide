"""CloudFront resources: an origin access control and a distribution.

Both are global (no ``region`` field) — CloudFront has a single global endpoint.
They are located by their provider-assigned id (``oac_id`` / ``distribution_id``),
restored from state for read/update/delete.
"""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws.resources.base import AwsResource, TaggedResource

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


class CloudFrontDistribution(TaggedResource):
    """A CloudFront distribution fronting a single S3 origin via an OAC.

    ``origin_domain`` (the bucket's regional domain) is immutable; everything
    else updates in place. ``domain_name`` is the ``*.cloudfront.net`` URL the
    site is served from.

    **Custom domains.** ``aliases`` are the names to serve, and
    ``certificate_arn`` is the ACM certificate proving them — pass
    ``cert.certificate_arn``. Both or neither: CloudFront rejects an alias with no
    certificate covering it, and a certificate with no alias serves nothing. The
    certificate must live in ``us-east-1``, which
    :class:`~atlantide.providers.aws.resources.certificate.AcmCertificate` already
    pins for you.
    """

    origin_domain: str = immutable()  # {bucket}.s3.{region}.amazonaws.com (a Ref)
    oac_id: str = mutable()  # a Ref to OriginAccessControl.oac_id
    default_root_object: str = mutable(default="index.html")
    enabled: bool = mutable(default=True)
    comment: str = mutable(default="")
    #: Domain names this distribution answers to (CNAMEs).
    aliases: list[str] = mutable(default_factory=list)
    #: ACM certificate ARN covering ``aliases``; must be in us-east-1.
    certificate_arn: str | None = mutable(default=None)
    #: Lowest TLS version accepted from viewers. The default excludes TLS 1.0/1.1.
    minimum_protocol_version: str = mutable(default="TLSv1.2_2021")
    #: ``PriceClass_All`` | ``PriceClass_200`` | ``PriceClass_100``.
    price_class: str = mutable(default="PriceClass_All")
    distribution_id: str = computed()
    domain_name: str = computed()  # <id>.cloudfront.net — the site URL
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> CloudFrontDistribution:
        if bool(self.aliases) != bool(self.certificate_arn):
            raise ValueError(
                "CloudFrontDistribution needs aliases and certificate_arn together: "
                "CloudFront rejects an alias with no certificate covering it, and a "
                "certificate with no alias serves nothing"
            )
        return self
