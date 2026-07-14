"""Reusable AWS L2 components (built from the L1 resources)."""

from __future__ import annotations

from atlantide.core import Component, child, current_stack_region
from atlantide.core.errors import RegistryError
from atlantide.providers.aws.policy import deny
from atlantide.providers.aws.resources import S3Bucket, S3BucketPolicy


class SecureBucket(Component):
    """A private S3 bucket plus a baseline hardening policy (TLS-only access).

    A worked example of a library component: two resources wired together — the
    policy *denies* every ``s3:*`` action on the bucket and its objects unless the
    request came over TLS (``aws:SecureTransport``), and depends on the bucket via
    its ``arn`` refs — namespaced under the component name. A ``Deny`` grants no
    public access, so it applies cleanly under S3 Block Public Access. Exposes the
    bucket handle and its computed ``arn`` / ``regional_domain_name``.
    """

    def __init__(
        self,
        name: str,
        *,
        bucket: str,
        region: str | None = None,
        versioning: bool = False,
    ) -> None:
        resolved = region or current_stack_region()
        if resolved is None:
            raise RegistryError("SecureBucket needs a region (pass region= or use a Stack)")
        self.bucket = child(
            S3Bucket, "assets", bucket=bucket, region=resolved, versioning=versioning
        )
        self.policy = child(
            S3BucketPolicy,
            "policy",
            bucket=self.bucket.bucket,
            statements=[
                deny(
                    "s3:*",
                    on=[self.bucket.arn, self.bucket.objects_arn],
                    principal="*",
                    condition={"Bool": {"aws:SecureTransport": "false"}},
                )
            ],
        )
        self.arn = self.bucket.arn
        self.domain_name = self.bucket.regional_domain_name
