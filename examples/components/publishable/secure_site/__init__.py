"""An example *publishable* component: put this in a public git repo and others
can ``atlantide component add`` it, then import it as ``atlantide.components.<alias>``.

A publishable component is ordinary trusted Python (config bans ``class``, so it
cannot be authored in Atlas-lang) that subclasses :class:`atlantide.core.Component`
and creates its children in ``__init__``. Children auto-namespace under the
instance name, so two instances never collide. Expose useful handles (here the
bucket and its ``arn``) as attributes for downstream wiring.

Layout for publishing: this package sits at ``secure_site/`` in the repo, so a
consumer runs ``atlantide component add <url> --subdir secure_site --as site``.
"""

from __future__ import annotations

from atlantide.core import Component, child, current_stack_region
from atlantide.core.errors import RegistryError
from atlantide.providers.aws.policy import deny
from atlantide.providers.aws.resources import S3Bucket, S3BucketPolicy


class SecureSite(Component):
    """A private, TLS-only S3 bucket for static-site assets.

    Mirrors the built-in ``aws.SecureBucket`` — a worked shape for a shared L2
    construct: a bucket plus a hardening policy that denies any non-TLS request.
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
            raise RegistryError("SecureSite needs a region (pass region= or use a Stack)")
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
