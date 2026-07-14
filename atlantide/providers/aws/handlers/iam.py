"""IAM handlers: roles and inline role policies."""

from __future__ import annotations

from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, tag_list
from atlantide.providers.aws.policy import assume_role, policy_json
from atlantide.providers.aws.resources import IamPolicy, IamRole


class IamRoleHandler(AwsHandler[IamRole]):
    service = "iam"
    resource_type = IamRole

    def create(self, client: Any, res: IamRole) -> dict[str, Any]:
        resp = client.create_role(
            RoleName=res.role_name,
            AssumeRolePolicyDocument=_trust_document(res),
            Description=res.description,
            Tags=tag_list(res.tags),
        )
        return {"arn": resp["Role"]["Arn"]}

    def read(self, client: Any, res: IamRole) -> dict[str, Any] | None:
        try:
            role = client.get_role(RoleName=res.role_name)
        except client.exceptions.NoSuchEntityException:
            return None
        return {"arn": role["Role"]["Arn"]}

    def update(self, client: Any, prior: dict[str, Any], res: IamRole) -> dict[str, Any]:
        client.update_assume_role_policy(
            RoleName=res.role_name, PolicyDocument=_trust_document(res)
        )
        client.update_role(RoleName=res.role_name, Description=res.description)
        if res.tags:
            client.tag_role(RoleName=res.role_name, Tags=tag_list(res.tags))
        return {"arn": client.get_role(RoleName=res.role_name)["Role"]["Arn"]}

    def delete(self, client: Any, res: IamRole) -> None:
        with ignore_missing():
            client.delete_role(RoleName=res.role_name)


def _trust_document(res: IamRole) -> str:
    """Role trust policy: the explicit JSON, or one built from ``assumed_by``."""
    if res.assume_role_policy is not None:
        return res.assume_role_policy
    assert res.assumed_by is not None  # guaranteed by IamRole's validator
    services = res.assumed_by if isinstance(res.assumed_by, list) else [res.assumed_by]
    return assume_role(*services)


class IamPolicyHandler(AwsHandler[IamPolicy]):
    service = "iam"
    resource_type = IamPolicy

    def create(self, client: Any, res: IamPolicy) -> dict[str, Any]:
        self._put(client, res)
        return {}

    def read(self, client: Any, res: IamPolicy) -> dict[str, Any] | None:
        try:
            client.get_role_policy(RoleName=_role_name(res.role_arn), PolicyName=res.policy_name)
        except client.exceptions.NoSuchEntityException:
            return None
        return {}

    def update(self, client: Any, prior: dict[str, Any], res: IamPolicy) -> dict[str, Any]:
        self._put(client, res)  # put_role_policy overwrites in place
        return {}

    @staticmethod
    def _put(client: Any, res: IamPolicy) -> None:
        client.put_role_policy(
            RoleName=_role_name(res.role_arn),
            PolicyName=res.policy_name,
            PolicyDocument=policy_json(res.statements),
        )

    def delete(self, client: Any, res: IamPolicy) -> None:
        with ignore_missing():  # the role and its inline policy may already be gone
            client.delete_role_policy(
                RoleName=_role_name(res.role_arn), PolicyName=res.policy_name
            )


def _role_name(role_arn: str) -> str:
    """Role name from an IAM role ARN (``arn:aws:iam::acct:role/NAME``)."""
    return role_arn.rsplit("/", 1)[-1]
