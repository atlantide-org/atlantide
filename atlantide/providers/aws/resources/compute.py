"""Compute resources: Lambda function."""

from __future__ import annotations

from atlantide.core import SecretRef, computed, immutable, mutable, secret
from atlantide.providers.aws.resources.base import AwsResource


class LambdaFunction(AwsResource):
    """An AWS Lambda function.

    ``function_name`` and ``region`` are immutable; ``role_arn`` (pass
    ``role.arn``), ``runtime``, ``handler`` and ``tags`` update in place. ``arn``
    is a computed output. Created with a placeholder zip.

    ``signing_secret`` holds a :class:`~atlantide.core.SecretRef` (a name, never a
    value) — surfaced to the function as the ``SIGNING_SECRET`` env var, resolved
    from the secrets backend at apply and redacted in plan/logs.
    """

    class Action:
        """IAM action constants, e.g. ``allow(LambdaFunction.Action.InvokeFunction, on=...)``."""

        InvokeFunction = "lambda:InvokeFunction"
        GetFunction = "lambda:GetFunction"

    function_name: str = immutable(physical_name=True)
    role_arn: str = mutable()
    runtime: str = mutable(default="python3.12")
    handler: str = mutable(default="index.handler")
    signing_secret: SecretRef | None = secret(default=None)
    region: str = immutable()  # required (from the stack region)
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()
