"""S3 resources: bucket and bucket policy."""

from __future__ import annotations

import re
from typing import Any

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_BUCKET_NAME = v.all_of(
    v.length_between(3, 63, "S3 bucket name"),
    v.forbids("..", "S3 bucket name"),
    v.matches(
        re.compile(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$"),
        "S3 bucket name",
        "lowercase letters, digits, '.' and '-'; must start and end alphanumeric",
    ),
)


class S3Bucket(AwsResource):
    """An S3 bucket.

    ``bucket`` (the globally-unique name) and ``region`` are immutable — changing
    either replaces the bucket. ``versioning`` and ``tags`` update in place.
    """

    class Action:
        """IAM action constants, e.g. ``allow(S3Bucket.Action.GetObject, on=...)``."""

        ListBucket = "s3:ListBucket"
        GetBucketLocation = "s3:GetBucketLocation"
        GetObject = "s3:GetObject"
        PutObject = "s3:PutObject"
        DeleteObject = "s3:DeleteObject"

    bucket: str = immutable(physical_name=True)
    region: str = immutable()  # required (from the stack region)
    versioning: bool = mutable(default=False)
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()  # the bucket ARN (arn:aws:s3:::name)
    objects_arn: str = computed()  # the objects ARN (arn:aws:s3:::name/*), for policies
    regional_domain_name: str = computed()  # {bucket}.s3.{region}.amazonaws.com (CloudFront origin)

    @model_validator(mode="after")
    def _validate(self) -> S3Bucket:
        v.check(self.bucket, _BUCKET_NAME)
        return self


class S3BucketPolicy(AwsResource):
    """A resource policy attached to an S3 bucket.

    ``bucket`` (pass ``bucket.bucket`` to depend on the bucket) is immutable;
    ``statements`` update in place. Build statements with ``allow()`` / ``deny()``
    and pass a ``principal`` (bucket policies require one).
    """

    bucket: str = immutable()
    statements: list[dict[str, Any]] = mutable()
