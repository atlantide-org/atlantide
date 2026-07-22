"""S3 handlers: buckets, bucket policies, and folder sync."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, tag_list
from atlantide.providers.aws.policy import policy_json
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import S3Bucket, S3BucketPolicy, S3Folder


class S3BucketHandler(AwsHandler[S3Bucket]):
    service = "s3"
    resource_type = S3Bucket

    def create(self, client: Any, res: S3Bucket) -> dict[str, Any]:
        try:
            # us-east-1 is the default and must omit the LocationConstraint,
            # which errors if sent; every other region must send it.
            if res.region == Region.UsEast1:
                client.create_bucket(Bucket=res.bucket)
            else:
                config: Any = {"LocationConstraint": res.region}
                client.create_bucket(Bucket=res.bucket, CreateBucketConfiguration=config)
        except client.exceptions.BucketAlreadyOwnedByYou:
            pass  # idempotent: bucket already owned
        return self._settings(client, res)

    def read(self, client: Any, res: S3Bucket) -> dict[str, Any] | None:
        try:
            client.head_bucket(Bucket=res.bucket)
        except client.exceptions.ClientError:
            return None
        # Observe the mutable inputs as well as the outputs, so refresh detects
        # in-place drift such as versioning toggled or tags edited out-of-band.
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
        "name": res.bucket,
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


class S3FolderHandler(AwsHandler[S3Folder]):
    service = "s3"
    resource_type = S3Folder

    def create(self, client: Any, res: S3Folder) -> dict[str, Any]:
        uploaded: dict[str, str] = {}
        for rel, digest in res.manifest.items():
            self._put(client, res, rel)
            uploaded[res.prefix + rel] = digest
        return {"uploaded": uploaded}

    def read(self, client: Any, res: S3Folder) -> dict[str, Any] | None:
        try:
            client.head_bucket(Bucket=res.bucket)
        except client.exceptions.ClientError:
            return None
        # Echo the pinned manifest from state. Per-object out-of-band drift is not
        # re-observed, since S3 ETags are not sha256 for multipart uploads.
        return {"uploaded": dict(_stored(res))}

    def update(self, client: Any, prior: dict[str, Any], res: S3Folder) -> dict[str, Any]:
        prior_keys: dict[str, str] = prior.get("uploaded") or {}
        desired = {res.prefix + rel: digest for rel, digest in res.manifest.items()}
        for rel, digest in res.manifest.items():
            if prior_keys.get(res.prefix + rel) != digest:
                self._put(client, res, rel)
        for key in prior_keys:
            if key not in desired:  # pruned locally -> remove from S3
                with ignore_missing():
                    client.delete_object(Bucket=res.bucket, Key=key)
        return {"uploaded": desired}

    def delete(self, client: Any, res: S3Folder) -> None:
        for key in _stored(res):
            with ignore_missing():
                client.delete_object(Bucket=res.bucket, Key=key)

    @staticmethod
    def _put(client: Any, res: S3Folder, rel: str) -> None:
        key = res.prefix + rel
        body = (Path(res.source_path) / rel).read_bytes()
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        extra: dict[str, Any] = {"CacheControl": res.cache_control} if res.cache_control else {}
        client.put_object(Bucket=res.bucket, Key=key, Body=body, ContentType=content_type, **extra)


def _stored(res: S3Folder) -> dict[str, str]:
    """The uploaded map restored from state, or ``{}`` before any apply."""
    value = res.uploaded
    return value if isinstance(value, dict) else {}
