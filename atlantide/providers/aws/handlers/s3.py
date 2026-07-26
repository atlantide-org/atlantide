"""S3 handlers: buckets, bucket policies, and folder sync."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    ignore_missing,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import absent_ok
from atlantide.providers.aws.policy import policy_json
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import S3Bucket, S3BucketPolicy, S3Folder


class S3BucketHandler(AwsHandler[S3Bucket]):
    service = "s3"
    resource_type = S3Bucket

    @override
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

    @override
    def read(self, client: Any, res: S3Bucket) -> dict[str, Any] | None:
        if absent_ok(lambda: client.head_bucket(Bucket=res.bucket)) is None:
            return None
        # Observe the mutable inputs as well as the outputs, so refresh detects
        # in-place drift such as versioning toggled or tags edited out-of-band.
        observed = _s3_outputs(res)
        observed["versioning"] = (
            client.get_bucket_versioning(Bucket=res.bucket).get("Status") == "Enabled"
        )
        observed["tags"] = _read_bucket_tags(client, res.bucket)
        observed["block_public_access"] = _read_public_access(client, res.bucket)
        observed["encryption"] = _read_encryption(client, res.bucket)
        return observed

    @override
    def update(self, client: Any, prior: dict[str, Any], res: S3Bucket) -> dict[str, Any]:
        return self._settings(client, res)

    @override
    def delete(self, client: Any, res: S3Bucket) -> None:
        with ignore_missing():
            if res.force_destroy:
                _empty_bucket(client, res.bucket)
            client.delete_bucket(Bucket=res.bucket)

    @staticmethod
    def _settings(client: Any, res: S3Bucket) -> dict[str, Any]:
        client.put_bucket_versioning(
            Bucket=res.bucket,
            VersioningConfiguration={"Status": "Enabled" if res.versioning else "Suspended"},
        )
        _apply_public_access(client, res)
        _apply_encryption(client, res)
        if res.tags:
            client.put_bucket_tagging(Bucket=res.bucket, Tagging={"TagSet": tag_list(res.tags)})
        else:
            client.delete_bucket_tagging(Bucket=res.bucket)
        return _s3_outputs(res)


def _apply_public_access(client: Any, res: S3Bucket) -> None:
    """Set (or clear) the bucket's public-access block.

    All four switches move together: leaving any one off leaves a route by which
    the bucket can be made public, which is not a state anyone means to be in.
    """
    if res.block_public_access:
        client.put_public_access_block(
            Bucket=res.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        return
    with ignore_missing():
        client.delete_public_access_block(Bucket=res.bucket)


def _apply_encryption(client: Any, res: S3Bucket) -> None:
    """Set (or clear) the bucket's default encryption."""
    if res.encryption is None:
        with ignore_missing():
            client.delete_bucket_encryption(Bucket=res.bucket)
        return
    rule: dict[str, Any] = {"SSEAlgorithm": res.encryption}
    if res.kms_key_id is not None:
        rule["KMSMasterKeyID"] = res.kms_key_id
    client.put_bucket_encryption(
        Bucket=res.bucket,
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": rule}]},
    )


def _empty_bucket(client: Any, bucket: str) -> None:
    """Delete every object *and every version* before the bucket itself.

    Versions matter: on a versioned bucket, deleting objects only adds delete
    markers, and ``delete_bucket`` still refuses. Paginated because a bucket can
    hold far more than one page, and a partial empty just fails the delete again
    with a less obvious message.
    """
    pages = client.get_paginator("list_object_versions").paginate(Bucket=bucket)
    for page in pages:
        doomed = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for key in ("Versions", "DeleteMarkers")
            for item in page.get(key, [])
        ]
        for batch in _batched(doomed, _DELETE_MAX):
            client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})


#: S3 caps one ``delete_objects`` call at 1000 keys.
_DELETE_MAX = 1000


def _batched(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _s3_outputs(res: S3Bucket) -> dict[str, Any]:
    arn = f"arn:aws:s3:::{res.bucket}"
    return {
        "name": res.bucket,
        "arn": arn,
        "objects_arn": f"{arn}/*",
        "bucket": res.bucket,
        "regional_domain_name": f"{res.bucket}.s3.{res.region}.amazonaws.com",
    }


def _read_public_access(client: Any, bucket: str) -> bool:
    """Whether all four public-access switches are on. Absent config reads False,
    which is what "not blocked" means."""
    try:
        config = client.get_public_access_block(Bucket=bucket)
    except ClientError:
        # S3 raises `NoSuchPublicAccessBlockConfiguration` when none is set, and
        # a caller lacking s3:GetBucketPublicAccessBlock raises too — neither is
        # a reason to fail a read, and both mean "cannot say it is blocked".
        return False
    settings = config.get("PublicAccessBlockConfiguration", {})
    return all(
        settings.get(key, False)
        for key in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
    )


def _read_encryption(client: Any, bucket: str) -> str | None:
    """The default SSE algorithm, or ``None`` when the bucket has no default."""
    try:
        config = client.get_bucket_encryption(Bucket=bucket)
    except ClientError:
        return None
    rules = config.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    for rule in rules:
        algorithm = rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
        if algorithm:
            return str(algorithm)
    return None


def _read_bucket_tags(client: Any, bucket: str) -> dict[str, str]:
    """Observed bucket tags, or ``{}`` when none are set (S3 raises, not empty)."""
    resp = absent_ok(lambda: client.get_bucket_tagging(Bucket=bucket))
    return tags_from_list(resp.get("TagSet", [])) if resp is not None else {}


class S3BucketPolicyHandler(AwsHandler[S3BucketPolicy]):
    service = "s3"
    resource_type = S3BucketPolicy

    @override
    def create(self, client: Any, res: S3BucketPolicy) -> dict[str, Any]:
        client.put_bucket_policy(Bucket=res.bucket, Policy=policy_json(res.statements))
        return {}

    @override
    def read(self, client: Any, res: S3BucketPolicy) -> dict[str, Any] | None:
        if absent_ok(lambda: client.get_bucket_policy(Bucket=res.bucket)) is None:
            return None
        return {}

    @override
    def update(self, client: Any, prior: dict[str, Any], res: S3BucketPolicy) -> dict[str, Any]:
        return self.create(client, res)  # put_bucket_policy overwrites in place

    @override
    def delete(self, client: Any, res: S3BucketPolicy) -> None:
        with ignore_missing():  # the bucket (and its policy) may already be gone
            client.delete_bucket_policy(Bucket=res.bucket)


class S3FolderHandler(AwsHandler[S3Folder]):
    service = "s3"
    resource_type = S3Folder

    @override
    def create(self, client: Any, res: S3Folder) -> dict[str, Any]:
        uploaded: dict[str, str] = {}
        for rel, digest in res.manifest.items():
            self._put(client, res, rel)
            uploaded[res.prefix + rel] = digest
        return {"uploaded": uploaded, "cache_control": res.cache_control}

    @override
    def read(self, client: Any, res: S3Folder) -> dict[str, Any] | None:
        if absent_ok(lambda: client.head_bucket(Bucket=res.bucket)) is None:
            return None
        # Echo the pinned manifest from state. Per-object out-of-band drift is not
        # re-observed, since S3 ETags are not sha256 for multipart uploads.
        return {"uploaded": dict(_stored(res))}

    @override
    def update(self, client: Any, prior: dict[str, Any], res: S3Folder) -> dict[str, Any]:
        prior_keys: dict[str, str] = prior.get("uploaded") or {}
        desired = {res.prefix + rel: digest for rel, digest in res.manifest.items()}
        # A metadata-only change (cache_control) leaves every digest equal, so
        # keying re-uploads on digests alone would record the new value in state
        # while every already-uploaded object kept the old header forever.
        metadata_changed = prior.get("cache_control") != res.cache_control
        for rel, digest in res.manifest.items():
            if metadata_changed or prior_keys.get(res.prefix + rel) != digest:
                self._put(client, res, rel)
        for key in prior_keys:
            if key not in desired:  # pruned locally -> remove from S3
                with ignore_missing():
                    client.delete_object(Bucket=res.bucket, Key=key)
        return {"uploaded": desired, "cache_control": res.cache_control}

    @override
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
