"""IAM handlers: roles and inline role policies."""

from __future__ import annotations

import contextlib
import json
from typing import Any, override
from urllib.parse import unquote

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    create_or_adopt,
    ignore_missing,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.policy import assume_role, policy_json
from atlantide.providers.aws.resources import IamPolicy, IamRole


class IamRoleHandler(AwsHandler[IamRole]):
    service = "iam"
    resource_type = IamRole

    @override
    def create(self, client: Client, res: IamRole) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            resp = client.create_role(
                RoleName=res.role_name,
                AssumeRolePolicyDocument=_trust_document(res),
                Description=res.description,
                Tags=tag_list(res.tags),
            )
            return {"arn": resp["Role"]["Arn"]}

        def adopt() -> dict[str, Any] | None:
            if self._outputs(client, res) is None:
                return None
            # The live role carries whatever trust policy, description and tags
            # it was created with; the update path makes them match config.
            return self.update(client, {}, res)

        # Adopt with the create-shaped outputs: `read` also reports the mutable
        # inputs, which stored as outputs would shadow those inputs on refresh.
        return create_or_adopt(make, adopt)

    def _outputs(self, client: Client, res: IamRole) -> dict[str, Any] | None:
        """The create-shaped outputs: the arn, or None if the role is absent."""
        try:
            return {"arn": client.get_role(RoleName=res.role_name)["Role"]["Arn"]}
        except client.exceptions.NoSuchEntityException:
            return None

    @override
    def read(self, client: Client, res: IamRole) -> dict[str, Any] | None:
        try:
            role = client.get_role(RoleName=res.role_name)["Role"]
        except client.exceptions.NoSuchEntityException:
            return None
        # The trust policy decides who may assume this role, so out-of-band edits
        # must surface. It is compared as a parsed document: AWS returns it
        # URL-encoded with reordered keys.
        observed: dict[str, Any] = {
            "arn": role["Arn"],
            # IAM omits the key when the description is empty; report "" rather
            # than echoing the desired value, so an out-of-band clear is visible.
            "description": role.get("Description", ""),
            "tags": tags_from_list(role.get("Tags", [])),
        }
        if (document := role.get("AssumeRolePolicyDocument")) is not None:
            observed["assume_role_policy"] = _reported_trust(document, res)
        return observed

    @override
    def update(self, client: Client, prior: dict[str, Any], res: IamRole) -> dict[str, Any]:
        client.update_assume_role_policy(
            RoleName=res.role_name, PolicyDocument=_trust_document(res)
        )
        client.update_role(RoleName=res.role_name, Description=res.description)
        _sync_role_tags(client, res)
        return {"arn": client.get_role(RoleName=res.role_name)["Role"]["Arn"]}

    @override
    def delete(self, client: Client, res: IamRole) -> None:
        with ignore_missing():
            client.delete_role(RoleName=res.role_name)


def _sync_role_tags(client: Client, res: IamRole) -> None:
    """Make the role's tags match config, removing any it no longer declares."""
    sync_tags(
        res.tags,
        live=lambda: tags_from_list(client.list_role_tags(RoleName=res.role_name)["Tags"]),
        untag=lambda stale, _: client.untag_role(RoleName=res.role_name, TagKeys=stale),
        tag=lambda tags: client.tag_role(RoleName=res.role_name, Tags=tag_list(tags)),
    )


def _reported_trust(live: Any, res: IamRole) -> Any:
    """The live trust policy, expressed the way this config states it.

    Config states the policy as ``assumed_by`` (service names) or
    ``assume_role_policy`` (JSON), while AWS returns a URL-encoded, key-reordered
    document, so a verbatim report never equals config. When the parsed documents
    match, the config value is reported; otherwise the live document is, so a
    console edit surfaces as drift.
    """
    if _normalised_policy(live) == _normalised_policy(_trust_document(res)):
        return res.assume_role_policy
    return _normalised_policy(live)


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

    @override
    def create(self, client: Client, res: IamPolicy) -> dict[str, Any]:
        self._put(client, res)
        return {}

    @override
    def read(self, client: Client, res: IamPolicy) -> dict[str, Any] | None:
        try:
            live = client.get_role_policy(
                RoleName=_role_name(res.role_arn), PolicyName=res.policy_name
            )
        except client.exceptions.NoSuchEntityException:
            return None
        # Report the statements so an out-of-band change to the policy shows as drift.
        document = _normalised_policy(live.get("PolicyDocument"))
        statements = document.get("Statement") if isinstance(document, dict) else None
        return {"statements": statements} if statements is not None else {}

    @override
    def update(self, client: Client, prior: dict[str, Any], res: IamPolicy) -> dict[str, Any]:
        self._put(client, res)  # put_role_policy overwrites in place
        return {}

    @staticmethod
    def _put(client: Client, res: IamPolicy) -> None:
        client.put_role_policy(
            RoleName=_role_name(res.role_arn),
            PolicyName=res.policy_name,
            PolicyDocument=policy_json(res.statements),
        )

    @override
    def delete(self, client: Client, res: IamPolicy) -> None:
        with ignore_missing():  # the role and its inline policy may already be gone
            client.delete_role_policy(RoleName=_role_name(res.role_arn), PolicyName=res.policy_name)


def _role_name(role_arn: str) -> str:
    """Role name from an IAM role ARN (``arn:aws:iam::acct:role/NAME``)."""
    return role_arn.rsplit("/", 1)[-1]


def _normalised_policy(document: Any) -> Any:
    """An IAM policy document as data, whatever shape AWS handed back.

    A document arrives either as a parsed dict or as a URL-encoded JSON string
    depending on the call, and its key order is not stable, so comparing the raw
    form would report drift on every run.
    """
    if isinstance(document, str):
        with contextlib.suppress(ValueError):
            return json.loads(unquote(document))
        return document
    return document
