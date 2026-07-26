"""Lambda handler."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typing_extensions import override

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    create_or_adopt,
    ignore_missing,
    sync_tags,
)
from atlantide.providers.aws.resources import LambdaFunction
from atlantide.providers.aws.resources.compute import package_bytes


class LambdaFunctionHandler(AwsHandler[LambdaFunction]):
    service = "lambda"
    resource_type = LambdaFunction

    @override
    def create(self, client: Any, res: LambdaFunction) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            resp = client.create_function(
                FunctionName=res.function_name,
                Runtime=res.runtime,
                Role=res.role_arn,
                Handler=res.handler,
                Code=_code(res),
                MemorySize=res.memory_size,
                Timeout=res.timeout,
                Tags=res.tags,
                **_lambda_env(res),
            )
            return {"arn": resp["FunctionArn"]}

        # Adopt to the *create* shape, not the read shape: `read` also reports the
        # mutable inputs, and storing those as outputs would shadow the inputs they
        # mirror on the next refresh.
        return create_or_adopt(make, lambda: self._outputs(client, res))

    def _outputs(self, client: Any, res: LambdaFunction) -> dict[str, Any] | None:
        """Just what a create returns: the arn, or None if there is no function."""
        try:
            resp = client.get_function(FunctionName=res.function_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        return {"arn": resp["Configuration"]["FunctionArn"]}

    @override
    def read(self, client: Any, res: LambdaFunction) -> dict[str, Any] | None:
        try:
            resp = client.get_function(FunctionName=res.function_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        config = resp["Configuration"]
        # Observe the mutable inputs alongside the arn, so refresh detects a handler
        # repointed or a timeout raised in the console instead of reporting an
        # unchecked "in sync".
        #
        # `CodeSha256` is deliberately absent: AWS reports it base64-encoded while
        # `code_sha256` is hex, so comparing them would flag drift on every run.
        # The coverage report names it as unchecked instead.
        # Only keys the API actually returned: falling back to the desired value
        # reports config as though it had been observed, which is exactly how
        # the refresh coverage machinery gets fooled (an omitted key is
        # *unchecked*, and refresh renders that truthfully).
        observed: dict[str, Any] = {"arn": config["FunctionArn"]}
        for name, key in (
            ("runtime", "Runtime"),
            ("handler", "Handler"),
            ("role_arn", "Role"),
            ("memory_size", "MemorySize"),
            ("timeout", "Timeout"),
        ):
            if key in config:
                observed[name] = config[key]
        return observed

    @override
    def update(self, client: Any, prior: dict[str, Any], res: LambdaFunction) -> dict[str, Any]:
        # Configuration and code are separate APIs; the code call uploads the
        # package, so it runs only when there is one to upload.
        resp = client.update_function_configuration(
            FunctionName=res.function_name,
            Role=res.role_arn,
            Runtime=res.runtime,
            Handler=res.handler,
            MemorySize=res.memory_size,
            Timeout=res.timeout,
            **_lambda_env(res),
        )
        arn = resp["FunctionArn"]
        if _has_code(res):
            # The configuration update leaves LastUpdateStatus=InProgress for a
            # while, and a code upload during it raises ResourceConflictException
            # on real AWS. Wait for the update to settle first.
            _wait_updated(client, res.function_name)
            client.update_function_code(FunctionName=res.function_name, **_code(res))
        sync_tags(
            res.tags,
            live=lambda: client.list_tags(Resource=arn).get("Tags", {}),
            untag=lambda stale, _: client.untag_resource(Resource=arn, TagKeys=stale),
            tag=lambda tags: client.tag_resource(Resource=arn, Tags=tags),
        )
        return {"arn": arn}

    @override
    def delete(self, client: Any, res: LambdaFunction) -> None:
        with ignore_missing():
            client.delete_function(FunctionName=res.function_name)


def _wait_updated(client: Any, function_name: str) -> None:
    """Block until the function's last update reaches a terminal status."""
    try:
        waiter = client.get_waiter("function_updated_v2")
    except ValueError:  # a botocore old enough to lack the v2 waiter
        waiter = client.get_waiter("function_updated")
    waiter.wait(FunctionName=function_name)


def _has_code(res: LambdaFunction) -> bool:
    return res.code_path is not None or res.s3_bucket is not None


def _code(res: LambdaFunction) -> dict[str, Any]:
    """The ``Code`` payload: the package this function is supposed to run.

    There is deliberately no default. A function created from a placeholder
    deploys, reports success, and then fails at its first invocation running code
    nobody wrote — the worst shape a failure can take, because every signal up to
    that point says it worked.
    """
    if res.s3_bucket is not None:
        code: dict[str, Any] = {"S3Bucket": res.s3_bucket, "S3Key": res.s3_key}
        if res.s3_object_version is not None:
            code["S3ObjectVersion"] = res.s3_object_version
        return code
    if res.code_path is None:
        raise ProviderError(
            f"lambda {res.function_name!r} has no code: pass code_path=<zip or "
            f"directory>, or s3_bucket=/s3_key= for a package already uploaded",
            op="create",
            resource_type=res.type_name(),
        )
    source = Path(res.code_path)
    if not source.exists():
        raise ProviderError(
            f"lambda {res.function_name!r}: code_path {res.code_path!r} does not "
            f"exist. A deploy from a built artifact cannot read local files — "
            f"upload the package and use s3_bucket=/s3_key= instead",
            op="create",
            resource_type=res.type_name(),
        )
    return {"ZipFile": package_bytes(source)}


def _lambda_env(res: LambdaFunction) -> dict[str, Any]:
    """The ``Environment`` kwarg: declared variables plus the signing secret.

    Always present, even when empty: omitting ``Environment`` from
    ``update_function_configuration`` leaves the live variables untouched, so an
    empty map is what clears a removed variable.
    """
    variables: dict[str, Any] = dict(res.environment)
    if res.signing_secret is not None:
        # Resolved to a plain string before the handler is reached; the field is
        # still typed as the reference it is in config.
        variables["SIGNING_SECRET"] = res.signing_secret
    return {"Environment": {"Variables": variables}}
