"""Lambda handler."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing
from atlantide.providers.aws.resources import LambdaFunction


class LambdaFunctionHandler(AwsHandler[LambdaFunction]):
    service = "lambda"
    resource_type = LambdaFunction

    def create(self, client: Any, res: LambdaFunction) -> dict[str, Any]:
        resp = client.create_function(
            FunctionName=res.function_name,
            Runtime=res.runtime,
            Role=res.role_arn,
            Handler=res.handler,
            Code={"ZipFile": _stub_zip()},
            Tags=res.tags,
            **_lambda_env(res),
        )
        return {"arn": resp["FunctionArn"]}

    def read(self, client: Any, res: LambdaFunction) -> dict[str, Any] | None:
        try:
            resp = client.get_function(FunctionName=res.function_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        return {"arn": resp["Configuration"]["FunctionArn"]}

    def update(self, client: Any, prior: dict[str, Any], res: LambdaFunction) -> dict[str, Any]:
        resp = client.update_function_configuration(
            FunctionName=res.function_name,
            Role=res.role_arn,
            Runtime=res.runtime,
            Handler=res.handler,
            **_lambda_env(res),
        )
        arn = resp["FunctionArn"]
        if res.tags:
            client.tag_resource(Resource=arn, Tags=res.tags)
        return {"arn": arn}

    def delete(self, client: Any, res: LambdaFunction) -> None:
        with ignore_missing():
            client.delete_function(FunctionName=res.function_name)


def _lambda_env(res: LambdaFunction) -> dict[str, Any]:
    """The ``Environment`` kwarg carrying the (unsealed) signing secret, if any."""
    if res.signing_secret is None:
        return {}
    return {"Environment": {"Variables": {"SIGNING_SECRET": res.signing_secret}}}


def _stub_zip() -> bytes:
    """A minimal deployment package placeholder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.py", "def handler(event, context):\n    return {}\n")
    return buffer.getvalue()
