"""Compute resources: Lambda function."""

from __future__ import annotations

import io
import stat
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from atlantide.core import Resource, SecretRef, computed, immutable, mutable, secret
from atlantide.core.errors import LanguageError
from atlantide.core.markers import contains_ref
from atlantide.providers.aws.resources.base import RegionalResource, TaggedResource


class LambdaFunction(RegionalResource, TaggedResource):
    """An AWS Lambda function.

    ``function_name`` and ``region`` are immutable; ``role_arn`` (pass
    ``role.arn``), ``runtime``, ``handler``, the package (including ``code_path``),
    ``memory_size``, ``timeout``, ``environment`` and ``tags`` update in place.
    ``arn`` is computed.

    **Code.** ``code_path`` names a local zip (or a directory, zipped
    deterministically); ``s3_bucket`` / ``s3_key`` name an object already in S3. A
    local package is fingerprinted at config-evaluation time into ``code_sha256``,
    so any byte change plans as an UPDATE. Moving the package to another
    ``code_path`` is an UPDATE too (re-uploading the same bytes), not a
    replacement. The provider reads the bytes at apply; a
    rehydrate (deploy) uses the artifact's pinned hash and never reads disk, as
    :class:`~atlantide.providers.aws.resources.s3.S3Folder` does.

    ``signing_secret`` holds a :class:`~atlantide.core.SecretRef` (a name, not a
    value). It is resolved from the secrets backend at apply, redacted in plan and
    logs, and set as the ``SIGNING_SECRET`` environment variable, overriding any
    ``environment`` entry of that name.
    """

    class Action:
        """IAM action constants, e.g. ``allow(LambdaFunction.Action.InvokeFunction, on=...)``."""

        InvokeFunction = "lambda:InvokeFunction"
        GetFunction = "lambda:GetFunction"

    function_name: str = immutable(physical_name=True)
    role_arn: str = mutable()
    runtime: str = mutable(default="python3.12")
    handler: str = mutable(default="index.handler")
    #: Local zip or directory to deploy. Mutually exclusive with ``s3_bucket``.
    #: Mutable: ``code_sha256`` carries the content, so a move is not a new function.
    code_path: str | None = mutable(default=None)
    #: sha256 of the ``code_path`` package; the input the diff compares.
    code_sha256: str = mutable(default="")
    #: Bucket of an already-uploaded package; requires ``s3_key``.
    s3_bucket: str | None = mutable(default=None)
    s3_key: str | None = mutable(default=None)
    s3_object_version: str | None = mutable(default=None)
    memory_size: int = mutable(default=128)
    timeout: int = mutable(default=3)
    environment: dict[str, str] = mutable(default_factory=dict)
    signing_secret: SecretRef | None = secret(default=None)
    arn: str = computed()

    def __init__(
        self,
        name: str,
        /,
        *,
        code_path: str | None = None,
        code_sha256: str | None = None,
        s3_bucket: str | None = None,
        s3_key: str | None = None,
        **data: Any,
    ) -> None:
        if code_path is not None and s3_bucket is not None:
            raise LanguageError("LambdaFunction takes either code_path or s3_bucket, not both")
        if s3_bucket is not None and s3_key is None:
            raise LanguageError("LambdaFunction.s3_bucket also needs s3_key")
        if code_sha256 is None:
            code_sha256 = _fingerprint(code_path) if code_path is not None else ""
        data.update(
            code_path=code_path,
            code_sha256=code_sha256,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
        )
        # Call the base initializer explicitly: mypy (no pydantic plugin) resolves
        # a bare super() to BaseModel.__init__ and loses the positional ``name``.
        Resource.__init__(self, name, **data)


def _fingerprint(path: str) -> str:
    """The sha256 of the package at ``path``: a zip file, or a directory zipped.

    Read at evaluation time, so the path must be a literal. Only the hash reaches
    the IR, so identical bytes give identical IR and a deploy from an artifact needs
    no filesystem.
    """
    if not isinstance(path, str) or contains_ref(path):
        raise LanguageError("LambdaFunction.code_path must be a literal path")
    source = Path(path)
    if not source.exists():
        raise LanguageError(f"LambdaFunction.code_path {path!r} does not exist")
    return sha256(package_bytes(source)).hexdigest()


def package_bytes(source: Path) -> bytes:
    """The deployment package for ``source``: its bytes if a zip, else a zip of it.

    Directories are zipped with sorted posix entry names, a fixed timestamp and a
    normalised mode, so the same tree always yields the same bytes and the
    fingerprint is stable across runs and platforms. Python caches are skipped as
    :class:`~atlantide.providers.aws.resources.s3.S3Folder` skips them: their bytes
    vary between runs and would plan spurious updates. An executable file keeps
    its exec bit (0o755), which a ``bootstrap`` for a ``provided.*`` runtime needs.
    """
    if source.is_file():
        return source.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(p for p in source.rglob("*") if p.is_file()):
            rel = entry.relative_to(source)
            if "__pycache__" in rel.parts or entry.suffix == ".pyc":
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=_EPOCH)
            executable = entry.stat().st_mode & stat.S_IXUSR
            info.external_attr = (0o755 if executable else 0o644) << 16
            archive.writestr(info, entry.read_bytes())
    return buffer.getvalue()


#: Fixed zip timestamp: real mtimes differ between checkouts of identical code.
_EPOCH = (1980, 1, 1, 0, 0, 0)
