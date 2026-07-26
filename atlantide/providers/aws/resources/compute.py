"""Compute resources: Lambda function."""

from __future__ import annotations

import io
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
    ``role.arn``), ``runtime``, ``handler``, ``code``, ``memory_size``,
    ``timeout``, ``environment`` and ``tags`` update in place. ``arn`` is a
    computed output.

    **Code.** ``code`` is either a local path to a zip (or a directory, zipped
    deterministically) or an object already in S3. A local source is fingerprinted
    at config-evaluation time into ``code_sha256``, an input that drives the diff:
    change a byte and the hash changes, so plan sees an UPDATE and apply ships the
    new package. Bytes are read by the provider at apply, so a rehydrate (deploy)
    uses the artifact's pinned hash and never touches disk — the same shape
    :class:`~atlantide.providers.aws.resources.s3.S3Folder` uses.

    ``signing_secret`` holds a :class:`~atlantide.core.SecretRef` (a name, never a
    value) — surfaced to the function as the ``SIGNING_SECRET`` env var, resolved
    from the secrets backend at apply and redacted in plan/logs. It is merged into
    ``environment``, which it overrides on a key clash.
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
    code_path: str | None = immutable(default=None)
    #: Digest of the package ``code_path`` names — the input the diff watches.
    code_sha256: str = mutable(default="")
    #: An already-uploaded package. ``s3_key`` is required alongside the bucket.
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
    """The digest of the package at ``path`` — a zip file, or a directory zipped.

    Read at evaluation time, which is why the path must be a literal: the whole
    point is that the *hash* is what reaches the IR, so two runs of the same
    config over the same bytes produce identical IR, and a deploy from an
    artifact needs no filesystem at all.
    """
    if not isinstance(path, str) or contains_ref(path):
        raise LanguageError("LambdaFunction.code_path must be a literal path")
    source = Path(path)
    if not source.exists():
        raise LanguageError(f"LambdaFunction.code_path {path!r} does not exist")
    return sha256(package_bytes(source)).hexdigest()


def package_bytes(source: Path) -> bytes:
    """The deployment package for ``source``: its bytes if a zip, else a zip of it.

    Directories are zipped with sorted entries and a fixed timestamp, so the same
    tree always produces the same bytes — otherwise the fingerprint would change
    on every run and every plan would show an update.
    """
    if source.is_file():
        return source.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(p for p in source.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(str(entry.relative_to(source)), date_time=_EPOCH)
            info.external_attr = 0o644 << 16
            archive.writestr(info, entry.read_bytes())
    return buffer.getvalue()


#: Fixed zip timestamp. Real mtimes would make the archive — and so the
#: fingerprint — differ between two checkouts of identical code.
_EPOCH = (1980, 1, 1, 0, 0, 0)
