"""S3 resources: bucket, bucket policy, and folder sync."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import model_validator

from atlantide.core import Resource, computed, immutable, mutable
from atlantide.core.errors import LanguageError
from atlantide.core.markers import contains_ref
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
    name: str = computed()  # the bucket name as a reference (orders dependents after create)
    arn: str = computed()  # the bucket ARN (arn:aws:s3:::name)
    objects_arn: str = computed()  # the objects ARN (arn:aws:s3:::name/*), for policies
    regional_domain_name: str = computed()  # {bucket}.s3.{region}.amazonaws.com (CloudFront origin)

    @model_validator(mode="after")
    def _validate(self) -> S3Bucket:
        v.check(self.bucket, _BUCKET_NAME)
        return self


def _dir_manifest(root: Path) -> dict[str, str]:
    """Map each file under ``root`` to its sha256, keyed by relative posix path.

    Sorted and excluding derived Python caches, so the mapping is deterministic
    and content changes (not just paths) enter the Merkle inputs.
    """
    if not root.is_dir():
        raise LanguageError(f"S3Folder.source_path is not a directory: {root}")
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


class S3Folder(AwsResource):
    """Sync a local directory into an S3 bucket under ``prefix`` (mirror, prunes).

    The directory is fingerprinted at config-evaluation time into ``manifest``
    (``{relpath: sha256}``), an input that drives the diff: any file add, change,
    or removal alters the manifest, so plan/apply see an UPDATE and re-sync only
    the delta. File bytes are read by the provider at apply. ``source_path`` must
    be a literal directory (the read precedes apply); a rehydrate (deploy) passes
    the artifact's pinned ``manifest`` and never touches disk.

    ``bucket`` is the target bucket name — pass the bucket's ``.name`` (a computed
    reference) to order the folder after the bucket; a literal name creates no
    dependency edge. ``bucket``, ``prefix``, and ``source_path`` are immutable;
    ``manifest`` and ``cache_control`` update in place. ``uploaded``
    (``{key: sha256}`` currently in S3) is a computed output.
    """

    bucket: str = immutable()  # bucket name; pass bucket.name to order after it
    region: str = immutable()  # required (from the stack region)
    prefix: str = immutable(default="")
    source_path: str = immutable()
    manifest: dict[str, str] = mutable(default_factory=dict)
    cache_control: str = mutable(default="")
    uploaded: dict[str, str] = computed()

    def __init__(
        self,
        name: str,
        /,
        *,
        bucket: str,
        source_path: str,
        prefix: str = "",
        manifest: dict[str, str] | None = None,
        cache_control: str = "",
        **data: Any,
    ) -> None:
        if manifest is None:
            if not isinstance(source_path, str) or contains_ref(source_path):
                raise LanguageError("S3Folder.source_path must be a literal directory path")
            manifest = _dir_manifest(Path(source_path))
        data.update(
            bucket=bucket,
            source_path=source_path,
            prefix=prefix,
            manifest=manifest,
            cache_control=cache_control,
        )
        # Call the base initializer explicitly: mypy (no pydantic plugin) resolves
        # a bare super() to BaseModel.__init__ and loses the positional ``name``.
        Resource.__init__(self, name, **data)


class S3BucketPolicy(AwsResource):
    """A resource policy attached to an S3 bucket.

    ``bucket`` (pass ``bucket.bucket`` to depend on the bucket) is immutable;
    ``statements`` update in place. Build statements with ``allow()`` / ``deny()``
    and pass a ``principal`` (bucket policies require one).
    """

    bucket: str = immutable()
    statements: list[dict[str, Any]] = mutable()
