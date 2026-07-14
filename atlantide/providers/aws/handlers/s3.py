"""S3 handlers: buckets and bucket policies."""

from __future__ import annotations

from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, tag_list
from atlantide.providers.aws.policy import policy_json
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import S3Bucket, S3BucketPolicy


class S3BucketHandler(AwsHandler[S3Bucket]):
    service = "s3"
    resource_type = S3Bucket

    def create(self, client: Any, res: S3Bucket) -> dict[str, Any]:
        try:
            # us-east-1 is the default: it must OMIT the LocationConstraint
            # (sending it errors), while every other region must send it.
            if res.region == Region.UsEast1:
                client.create_bucket(Bucket=res.bucket)
            else:
                config: Any = {"LocationConstraint": res.region}
                client.create_bucket(Bucket=res.bucket, CreateBucketConfiguration=config)
        except client.exceptions.BucketAlreadyOwnedByYou:
            pass  # idempotent: we already own it
        return self._settings(client, res)

    def read(self, client: Any, res: S3Bucket) -> dict[str, Any] | None:
        try:
            client.head_bucket(Bucket=res.bucket)
        except client.exceptions.ClientError:
            return None
        # Also observe the mutable inputs so refresh detects in-place drift
        # (versioning toggled / tags edited out-of-band), not just outputs.
        observed = _s3_outputs(res)
        observed["versioning"] = (
            client.get_bucket_versioning(Bucket=res.bucket).get("Status") == "Enabled"
        )
        observed["tags"] = _read_bucket_tags(client, res.bucket)
        return observed

    def update(self, client: Any, prior: dict[str, Any], res: S3Bucket) -> dict[str, Any]:
        return self._settings(client, res)

    def delete(self, client: Any, res: S3Bucket) -> None:
        with ignore_missing():
            client.delete_bucket(Bucket=res.bucket)

    @staticmethod
    def _settings(client: Any, res: S3Bucket) -> dict[str, Any]:
        client.put_bucket_versioning(
            Bucket=res.bucket,
            VersioningConfiguration={"Status": "Enabled" if res.versioning else "Suspended"},
        )
        if res.tags:
            client.put_bucket_tagging(Bucket=res.bucket, Tagging={"TagSet": tag_list(res.tags)})
        else:
            client.delete_bucket_tagging(Bucket=res.bucket)
        return _s3_outputs(res)


def _s3_outputs(res: S3Bucket) -> dict[str, Any]:
    arn = f"arn:aws:s3:::{res.bucket}"
    return {
        "arn": arn,
        "objects_arn": f"{arn}/*",
        "bucket": res.bucket,
        "regional_domain_name": f"{res.bucket}.s3.{res.region}.amazonaws.com",
    }


def _read_bucket_tags(client: Any, bucket: str) -> dict[str, str]:
    """Observed bucket tags, or ``{}`` when none are set (S3 raises, not empty)."""
    try:
        resp = client.get_bucket_tagging(Bucket=bucket)
    except client.exceptions.ClientError:
        return {}
    return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}


class S3BucketPolicyHandler(AwsHandler[S3BucketPolicy]):
    service = "s3"
    resource_type = S3BucketPolicy

    def create(self, client: Any, res: S3BucketPolicy) -> dict[str, Any]:
        client.put_bucket_policy(Bucket=res.bucket, Policy=policy_json(res.statements))
        return {}

    def read(self, client: Any, res: S3BucketPolicy) -> dict[str, Any] | None:
        try:
            client.get_bucket_policy(Bucket=res.bucket)
        except client.exceptions.ClientError:
            return None
        return {}

    def update(self, client: Any, prior: dict[str, Any], res: S3BucketPolicy) -> dict[str, Any]:
        return self.create(client, res)  # put_bucket_policy overwrites in place

    def delete(self, client: Any, res: S3BucketPolicy) -> None:
        with ignore_missing():  # the bucket (and its policy) may already be gone
            client.delete_bucket_policy(Bucket=res.bucket)
