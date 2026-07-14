"""IAM resources: role and inline role policy."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_ROLE_NAME_MAX = 64


class IamRole(AwsResource):
    """An IAM role (global, no region).

    ``role_name`` is immutable; the trust policy, description, and tags update in
    place. ``arn`` is a computed output.

    Specify the trust policy exactly one way: ``assumed_by`` with a service
    principal (or list), or ``assume_role_policy`` with a ready-made JSON string
    (see :func:`~atlantide.providers.aws.policy.assume_role`).
    """

    class Action:
        """IAM action constants, e.g. ``allow(IamRole.Action.PassRole, on=...)``."""

        PassRole = "iam:PassRole"
        GetRole = "iam:GetRole"
        TagRole = "iam:TagRole"

    role_name: str = immutable(physical_name=True)
    assumed_by: str | list[str] | None = mutable(default=None)
    assume_role_policy: str | None = mutable(default=None)
    description: str = mutable(default="")
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> IamRole:
        if (self.assumed_by is None) == (self.assume_role_policy is None):
            raise ValueError(
                "IamRole needs exactly one of 'assumed_by' or 'assume_role_policy'"
            )
        v.check(self.role_name, v.max_length(_ROLE_NAME_MAX, "role_name"))
        return self


class IamPolicy(AwsResource):
    """An inline permissions policy embedded in an IAM role (global, no region).

    ``role_arn`` names the role the policy attaches to (pass ``role.arn`` to make
    the policy depend on the role) and ``policy_name`` identifies it within that
    role; both are immutable (changing either replaces the policy).

    ``statements`` is a list of statement dicts, built with the ``allow()`` /
    ``deny()`` helpers in :mod:`atlantide.providers.aws.policy`; the provider
    serializes them into an IAM policy document. It updates in place.
    """

    role_arn: str = immutable()
    policy_name: str = immutable()
    statements: list[dict[str, Any]] = mutable()
