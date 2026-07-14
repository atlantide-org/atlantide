"""Builders for IAM policy statements.

Compose an :class:`IamPolicy` from ``allow(...)`` / ``deny(...)`` calls; the
provider serializes the statements to a policy document at apply time (see
``handlers._policy_document``).

A ``resource`` may be an ARN string, a list of ARNs, or a ``Ref`` to an upstream
output (e.g. ``bucket.arn``); nested Refs become dependency edges and resolve to
concrete ARNs before the policy is written.
"""

from __future__ import annotations

import json
from typing import Any

Statement = dict[str, Any]

_VERSION = "2012-10-17"


class ServicePrincipal:
    """AWS service-principal constants for role trust policies.

    Use with ``IamRole(assumed_by=ServicePrincipal.Ec2)`` or
    ``assume_role(ServicePrincipal.Lambda)``.
    """

    Ec2 = "ec2.amazonaws.com"
    Lambda = "lambda.amazonaws.com"
    Ecs = "ecs.amazonaws.com"
    EcsTasks = "ecs-tasks.amazonaws.com"
    Events = "events.amazonaws.com"
    ApiGateway = "apigateway.amazonaws.com"
    StepFunctions = "states.amazonaws.com"
    Sns = "sns.amazonaws.com"
    CloudFront = "cloudfront.amazonaws.com"


def allow(
    *actions: str,
    on: Any,
    principal: Any = None,
    sid: str | None = None,
    condition: dict[str, Any] | None = None,
) -> Statement:
    """An ``Allow`` statement for ``actions`` on the ``on`` resource(s).

    ``principal`` is required by resource policies (e.g. S3 bucket policies:
    ``"*"`` or ``{"AWS": role.arn}``) and omitted from identity policies.
    ``condition`` adds a ``Condition`` block — e.g. a CloudFront OAC bucket policy
    scopes access to one distribution with
    ``condition={"StringEquals": {"AWS:SourceArn": dist.arn}}``.
    """
    return _statement("Allow", actions, on, principal, sid, condition)


def deny(
    *actions: str,
    on: Any,
    principal: Any = None,
    sid: str | None = None,
    condition: dict[str, Any] | None = None,
) -> Statement:
    """A ``Deny`` statement for ``actions`` on the ``on`` resource(s)."""
    return _statement("Deny", actions, on, principal, sid, condition)


def _statement(
    effect: str,
    actions: tuple[str, ...],
    on: Any,
    principal: Any,
    sid: str | None,
    condition: dict[str, Any] | None = None,
) -> Statement:
    if not actions:
        raise ValueError(f"{effect} statement needs at least one action")
    statement: Statement = {"Effect": effect, "Action": list(actions), "Resource": on}
    if principal is not None:
        statement["Principal"] = principal
    if sid is not None:
        statement["Sid"] = sid
    if condition is not None:
        statement["Condition"] = condition
    return statement


def policy_document(statements: list[Statement]) -> dict[str, Any]:
    """Wrap statements in an IAM policy document (``{Version, Statement}``)."""
    return {"Version": _VERSION, "Statement": statements}


def policy_json(statements: list[Statement]) -> str:
    """Serialize policy statements to a deterministic IAM policy-document JSON."""
    return json.dumps(policy_document(statements), sort_keys=True, separators=(",", ":"))


def assume_role(*services: str) -> str:
    """Trust-policy JSON string letting the given service principal(s) assume a role.

    E.g. ``assume_role("lambda.amazonaws.com")``. Pass to
    ``IamRole(assume_role_policy=...)``, or use ``IamRole(assumed_by=...)``.
    """
    if not services:
        raise ValueError("assume_role needs at least one service principal")
    principal = services[0] if len(services) == 1 else list(services)
    document = policy_document(
        [{"Effect": "Allow", "Principal": {"Service": principal}, "Action": "sts:AssumeRole"}]
    )
    return json.dumps(document, sort_keys=True, separators=(",", ":"))
