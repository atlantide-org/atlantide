"""EC2 networking handlers: VPCs, subnets, security groups."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, TypeVar

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler, known_id, tag_list
from atlantide.providers.aws.resources import Ec2Resource, SecurityGroup, Subnet, Vpc

E = TypeVar("E", bound=Ec2Resource)


class Ec2Handler(AwsHandler[E]):
    """CRUD for an EC2 resource located by its id, with attribute lookup as fallback.

    EC2 has no name-based ``get``, so a resource with no known id is discovered by
    its attributes (``_find``). Update and delete act on the id persisted in state;
    attribute lookup is a fallback only, since attributes such as a VPC's CIDR are
    not unique. A subclass supplies the id field name (``id_key``), ``_create``,
    ``_find``, and ``_delete``. Tags are (re)applied on create and update.
    """

    service = "ec2"
    id_key: ClassVar[str]

    def _known_id(self, res: E) -> str | None:
        """This resource's real id from state, or None when not yet known."""
        return known_id(res, self.id_key)

    @abstractmethod
    def _create(self, client: Any, res: E) -> str:
        """Create the resource and return its id."""

    @abstractmethod
    def _find(self, client: Any, res: E) -> str | None:
        """Resolve the resource's id from its attributes, or None if it is absent."""

    @abstractmethod
    def _delete(self, client: Any, resource_id: str) -> None:
        """Delete the resource by id."""

    def create(self, client: Any, res: E) -> dict[str, Any]:
        resource_id = self._create(client, res)
        _ec2_tag(client, resource_id, res.tags)
        return {self.id_key: resource_id}

    def read(self, client: Any, res: E) -> dict[str, Any] | None:
        resource_id = self._find(client, res)
        return None if resource_id is None else {self.id_key: resource_id}

    def update(self, client: Any, prior: dict[str, Any], res: E) -> dict[str, Any]:
        # Act on the id persisted in state; attribute lookup is a fallback only.
        resource_id = prior.get(self.id_key) or self._known_id(res) or self._find(client, res)
        if resource_id is None:  # update runs only on an existing resource
            raise ProviderError(
                f"{res.type_name()} not found by its attributes",
                op="update", resource_type=res.type_name(),
            )
        _ec2_tag(client, resource_id, res.tags)
        return {self.id_key: resource_id}

    def delete(self, client: Any, res: E) -> None:
        # Delete the id restored from state onto ``id_key``; ``_find`` is a fallback.
        resource_id = self._known_id(res) or self._find(client, res)
        if resource_id is not None:
            self._delete(client, resource_id)


class VpcHandler(Ec2Handler[Vpc]):
    resource_type = Vpc
    id_key = "vpc_id"

    def _create(self, client: Any, res: Vpc) -> str:
        return str(client.create_vpc(CidrBlock=res.cidr_block)["Vpc"]["VpcId"])

    def _find(self, client: Any, res: Vpc) -> str | None:
        resp = client.describe_vpcs(Filters=[{"Name": "cidr", "Values": [res.cidr_block]}])
        vpcs = resp.get("Vpcs", [])
        return str(vpcs[0]["VpcId"]) if vpcs else None

    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_vpc(VpcId=resource_id)


class SubnetHandler(Ec2Handler[Subnet]):
    resource_type = Subnet
    id_key = "subnet_id"

    def _create(self, client: Any, res: Subnet) -> str:
        resp = client.create_subnet(VpcId=res.vpc_id, CidrBlock=res.cidr_block)
        return str(resp["Subnet"]["SubnetId"])

    def _find(self, client: Any, res: Subnet) -> str | None:
        resp = client.describe_subnets(
            Filters=[
                {"Name": "cidr-block", "Values": [res.cidr_block]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ]
        )
        subnets = resp.get("Subnets", [])
        return str(subnets[0]["SubnetId"]) if subnets else None

    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_subnet(SubnetId=resource_id)


class SecurityGroupHandler(Ec2Handler[SecurityGroup]):
    resource_type = SecurityGroup
    id_key = "group_id"

    def _create(self, client: Any, res: SecurityGroup) -> str:
        resp = client.create_security_group(
            GroupName=res.group_name, Description=res.description, VpcId=res.vpc_id
        )
        return str(resp["GroupId"])

    def _find(self, client: Any, res: SecurityGroup) -> str | None:
        resp = client.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [res.group_name]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ]
        )
        groups = resp.get("SecurityGroups", [])
        return str(groups[0]["GroupId"]) if groups else None

    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_security_group(GroupId=resource_id)


def _ec2_tag(client: Any, resource_id: str, tags: dict[str, str]) -> None:
    if tags:
        client.create_tags(Resources=[resource_id], Tags=tag_list(tags))
