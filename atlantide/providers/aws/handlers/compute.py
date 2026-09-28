"""Lambda handler."""

from __future__ import annotations

from pathlib import Path
from typing import Any, override

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
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
    def create(self, client: Client, res: LambdaFunction) -> dict[str, Any]:
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

        def adopt() -> dict[str, Any] | None:
            if self._outputs(client, res) is None:
                return None  # vanished between the conflict and the read
            # The adopted function keeps whatever configuration and code it had,
            # while state records the declared inputs, so the next plan would see
            # no diff. Converge it now, as an update would. A function created
            # moments ago is still Pending and rejects configuration changes.
            _wait_active(client, res.function_name)
            return self.update(client, {}, res)

        # Adopt with the create-shaped outputs (`update` returns them too): `read`
        # also reports the mutable inputs, which stored as outputs would shadow
        # those inputs on refresh.
        return create_or_adopt(make, adopt)

    def _outputs(self, client: Client, res: LambdaFunction) -> dict[str, Any] | None:
        """The create-shaped outputs: the arn, or None if there is no function."""
        try:
            resp = client.get_function(FunctionName=res.function_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        return {"arn": resp["Configuration"]["FunctionArn"]}

    @override
    def read(self, client: Client, res: LambdaFunction) -> dict[str, Any] | None:
        try:
            resp = client.get_function(FunctionName=res.function_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        config = resp["Configuration"]
        # Report the mutable inputs alongside the arn so refresh detects console
        # edits. Only keys the API returned are reported; an omitted key shows as
        # unchecked in refresh coverage. `CodeSha256` is omitted: AWS reports it
        # base64-encoded while `code_sha256` is hex.
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
    def update(self, client: Client, prior: dict[str, Any], res: LambdaFunction) -> dict[str, Any]:
        # Configuration and code are separate APIs; the code call runs only when
        # there is a package to upload.
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
            # A code upload while LastUpdateStatus=InProgress raises
            # ResourceConflictException, so wait for the configuration update.
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
    def delete(self, client: Client, res: LambdaFunction) -> None:
        with ignore_missing():
            client.delete_function(FunctionName=res.function_name)


def _wait_updated(client: Client, function_name: str) -> None:
    """Block until the function's last update reaches a terminal status."""
    try:
        waiter = client.get_waiter("function_updated_v2")
    except ValueError:  # botocore versions without the v2 waiter
        waiter = client.get_waiter("function_updated")
    waiter.wait(FunctionName=function_name)


def _wait_active(client: Client, function_name: str) -> None:
    """Block until the function leaves ``Pending`` (a fresh create)."""
    try:
        waiter = client.get_waiter("function_active_v2")
    except ValueError:  # botocore versions without the v2 waiter
        waiter = client.get_waiter("function_active")
    waiter.wait(FunctionName=function_name)


def _has_code(res: LambdaFunction) -> bool:
    return res.code_path is not None or res.s3_bucket is not None


def _code(res: LambdaFunction) -> dict[str, Any]:
    """The ``Code`` payload: the package this function is supposed to run.

    There is no placeholder default: a function created from one deploys
    successfully and fails at its first invocation.
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
    ``update_function_configuration`` leaves the live variables untouched; an
    empty map clears removed variables.
    """
    variables: dict[str, Any] = dict(res.environment)
    if res.signing_secret is not None:
        # Resolved to a plain string before the handler is reached; the field is
        # still typed as the reference it is in config.
        variables["SIGNING_SECRET"] = res.signing_secret
    return {"Environment": {"Variables": variables}}
