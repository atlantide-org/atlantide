"""EC2 networking handlers: VPCs, subnets, security groups, and routing.

The shared EC2 base is in :mod:`.ec2`; security-group rule translation is in
:mod:`.sgrules`.
"""

from __future__ import annotations

from typing import Any, override

from atlantide.providers.aws.handlers.base import Client
from atlantide.providers.aws.handlers.ec2 import (
    Ec2Handler,
    tag_spec,
)
from atlantide.providers.aws.handlers.sgrules import (
    atomic_units,
    has_rule,
    redescribed,
    rule_to_aws,
    rules_from_aws,
)
from atlantide.providers.aws.resources import SecurityGroup, Subnet, Vpc
from atlantide.providers.aws.resources.networking import (
    ElasticIp,
    InternetGateway,
    NatGateway,
    Route,
    RouteTable,
    SgRule,
)


def _reported_rules(live: list[dict[str, Any]], declared: list[SgRule]) -> list[dict[str, Any]]:
    """The live rules, expressed the way this config states them.

    EC2 merges rules sharing protocol and ports and returns ranges in no stable
    order, so a verbatim report rarely equals config. When both sides normalize
    to the same single-source rules (see :func:`rules_from_aws`) the declared
    rules are reported; otherwise the live ones are, so a console edit surfaces
    as drift.
    """
    if live == rules_from_aws([rule_to_aws(rule) for rule in declared]):
        return [rule.model_dump() for rule in declared]
    return live


class VpcHandler(Ec2Handler[Vpc]):
    resource_type = Vpc
    identity_field = "vpc_id"
    describe_call = "describe_vpcs"
    list_key = "Vpcs"
    id_key = "VpcId"
    ids_kwarg = "VpcIds"

    @override
    def _create(self, client: Client, res: Vpc) -> str:
        resp = client.create_vpc(
            CidrBlock=res.cidr_block, TagSpecifications=tag_spec("vpc", res.node_id)
        )
        return str(resp["Vpc"]["VpcId"])

    @override
    def _find(self, client: Client, res: Vpc) -> str | None:
        return self._first_id(client, Filters=[{"Name": "cidr", "Values": [res.cidr_block]}])

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        client.delete_vpc(VpcId=resource_id)


class SubnetHandler(Ec2Handler[Subnet]):
    resource_type = Subnet
    identity_field = "subnet_id"
    describe_call = "describe_subnets"
    list_key = "Subnets"
    id_key = "SubnetId"
    ids_kwarg = "SubnetIds"

    @override
    def _create(self, client: Client, res: Subnet) -> str:
        kwargs: dict[str, Any] = {
            "VpcId": res.vpc_id,
            "CidrBlock": res.cidr_block,
            "TagSpecifications": tag_spec("subnet", res.node_id),
        }
        if res.availability_zone is not None:
            kwargs["AvailabilityZone"] = res.availability_zone
        return str(client.create_subnet(**kwargs)["Subnet"]["SubnetId"])

    @override
    def _after_create(self, client: Client, resource_id: str, res: Subnet) -> None:
        self._set_public_ip(client, resource_id, res)

    @override
    def update(self, client: Client, prior: dict[str, Any], res: Subnet) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._set_public_ip(client, str(outputs[self.identity_field]), res)
        return outputs

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        # A key AWS omits is left out, so refresh treats it as unchecked.
        return {
            name: caster(live[key])
            for name, key, caster in (
                ("availability_zone", "AvailabilityZone", str),
                ("map_public_ip_on_launch", "MapPublicIpOnLaunch", bool),
            )
            if key in live
        }

    @staticmethod
    def _set_public_ip(client: Client, subnet_id: str, res: Subnet) -> None:
        client.modify_subnet_attribute(
            SubnetId=subnet_id,
            MapPublicIpOnLaunch={"Value": res.map_public_ip_on_launch},
        )

    @override
    def _find(self, client: Client, res: Subnet) -> str | None:
        return self._first_id(
            client,
            Filters=[
                {"Name": "cidr-block", "Values": [res.cidr_block]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ],
        )

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        client.delete_subnet(SubnetId=resource_id)


class SecurityGroupHandler(Ec2Handler[SecurityGroup]):
    resource_type = SecurityGroup
    identity_field = "group_id"
    describe_call = "describe_security_groups"
    list_key = "SecurityGroups"
    id_key = "GroupId"
    ids_kwarg = "GroupIds"

    @override
    def _create(self, client: Client, res: SecurityGroup) -> str:
        resp = client.create_security_group(
            GroupName=res.group_name,
            Description=res.description,
            VpcId=res.vpc_id,
            TagSpecifications=tag_spec("security-group", res.node_id),
        )
        return str(resp["GroupId"])

    @override
    def _after_create(self, client: Client, resource_id: str, res: SecurityGroup) -> None:
        self._sync_rules(client, resource_id, res)

    @override
    def update(self, client: Client, prior: dict[str, Any], res: SecurityGroup) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._sync_rules(client, str(outputs[self.identity_field]), res)
        return outputs

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        # Rules are reported so console edits show as drift. An absent key is
        # unchecked; an empty list means no rules.
        return {
            name: rules_from_aws(live[key])
            for name, key in (("ingress", "IpPermissions"), ("egress", "IpPermissionsEgress"))
            if key in live
        }

    @override
    def read(self, client: Client, res: SecurityGroup) -> dict[str, Any] | None:
        observed = super().read(client, res)
        if observed is not None:
            for name, declared in (("ingress", res.ingress), ("egress", res.egress)):
                if name in observed:
                    observed[name] = _reported_rules(observed[name], declared)
        return observed

    def _sync_rules(self, client: Client, group_id: str, res: SecurityGroup) -> None:
        """Make the live rules match the declared ones, in both directions.

        AWS has no call to replace a rule set, so the delta is applied as an
        authorize plus a revoke; the revoke closes rules removed from config.

        A new group starts with an allow-all egress rule, so ``egress=[]`` revokes it.
        """
        live = client.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
        for direction, key, authorize, revoke, redescribe in (
            (
                res.ingress,
                "IpPermissions",
                client.authorize_security_group_ingress,
                client.revoke_security_group_ingress,
                client.update_security_group_rule_descriptions_ingress,
            ),
            (
                res.egress,
                "IpPermissionsEgress",
                client.authorize_security_group_egress,
                client.revoke_security_group_egress,
                client.update_security_group_rule_descriptions_egress,
            ),
        ):
            # Compared per one-range unit because EC2 merges live ranges into one
            # permission per (protocol, from, to). See :func:`atomic_units`.
            desired = atomic_units([rule_to_aws(rule) for rule in direction])
            current = atomic_units(live.get(key, []))
            if added := [p for p in desired if not has_rule(current, p)]:
                authorize(GroupId=group_id, IpPermissions=added)
            if stale := [p for p in current if not has_rule(desired, p)]:
                revoke(GroupId=group_id, IpPermissions=stale)
            if relabelled := redescribed(desired, current):
                redescribe(GroupId=group_id, IpPermissions=relabelled)

    @override
    def _find(self, client: Client, res: SecurityGroup) -> str | None:
        return self._first_id(
            client,
            Filters=[
                {"Name": "group-name", "Values": [res.group_name]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ],
        )

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        client.delete_security_group(GroupId=resource_id)


class InternetGatewayHandler(Ec2Handler[InternetGateway]):
    resource_type = InternetGateway
    identity_field = "internet_gateway_id"
    describe_call = "describe_internet_gateways"
    list_key = "InternetGateways"
    id_key = "InternetGatewayId"
    ids_kwarg = "InternetGatewayIds"

    @override
    def _create(self, client: Client, res: InternetGateway) -> str:
        resp = client.create_internet_gateway(
            TagSpecifications=tag_spec("internet-gateway", res.node_id)
        )
        return str(resp["InternetGateway"]["InternetGatewayId"])

    @override
    def _after_create(self, client: Client, resource_id: str, res: InternetGateway) -> None:
        # An adopted gateway may already be attached.
        described = client.describe_internet_gateways(InternetGatewayIds=[resource_id])
        attachments = described["InternetGateways"][0].get("Attachments", [])
        if not any(a.get("VpcId") == res.vpc_id for a in attachments):
            client.attach_internet_gateway(InternetGatewayId=resource_id, VpcId=res.vpc_id)

    @override
    def _find(self, client: Client, res: InternetGateway) -> str | None:
        return self._first_id(
            client, Filters=[{"Name": "attachment.vpc-id", "Values": [res.vpc_id]}]
        )

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        # AWS refuses to delete an attached gateway. The attached VPC is read back
        # because config may no longer describe it.
        described = client.describe_internet_gateways(InternetGatewayIds=[resource_id])
        for attachment in described["InternetGateways"][0].get("Attachments", []):
            client.detach_internet_gateway(InternetGatewayId=resource_id, VpcId=attachment["VpcId"])
        client.delete_internet_gateway(InternetGatewayId=resource_id)


class ElasticIpHandler(Ec2Handler[ElasticIp]):
    resource_type = ElasticIp
    identity_field = "allocation_id"
    describe_call = "describe_addresses"
    list_key = "Addresses"
    id_key = "AllocationId"
    ids_kwarg = "AllocationIds"

    @override
    def _create(self, client: Client, res: ElasticIp) -> str:
        resp = client.allocate_address(
            Domain="vpc", TagSpecifications=tag_spec("elastic-ip", res.node_id)
        )
        return str(resp["AllocationId"])

    @override
    def create(self, client: Client, res: ElasticIp) -> dict[str, Any]:
        outputs = super().create(client, res)
        return {**outputs, **self._address(client, str(outputs[self.identity_field]))}

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        return {"public_ip": live["PublicIp"]} if "PublicIp" in live else {}

    @staticmethod
    def _address(client: Client, allocation_id: str) -> dict[str, Any]:
        found = client.describe_addresses(AllocationIds=[allocation_id])["Addresses"]
        return {"public_ip": found[0].get("PublicIp", "")} if found else {}

    @override
    def _find(self, client: Client, res: ElasticIp) -> str | None:
        # An address has no attributes to match on, so the node tag is its only
        # identity. An unmanaged address is found only by the state id.
        return self._find_tagged(client, res.node_id)

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        client.release_address(AllocationId=resource_id)


#: A deleted or failed NAT gateway stays visible in the API for a while. `_is_live`
#: excludes these states so a destroyed gateway does not read as present and a
#: failed one is never adopted.
_DEAD_NAT_STATES = frozenset({"deleted", "deleting", "failed"})


class NatGatewayHandler(Ec2Handler[NatGateway]):
    resource_type = NatGateway
    identity_field = "nat_gateway_id"
    describe_call = "describe_nat_gateways"
    list_key = "NatGateways"
    id_key = "NatGatewayId"
    ids_kwarg = "NatGatewayIds"
    filters_kwarg = "Filter"

    @staticmethod
    @override
    def _is_live(item: dict[str, Any]) -> bool:
        return item.get("State") not in _DEAD_NAT_STATES

    @override
    def _create(self, client: Client, res: NatGateway) -> str:
        resp = client.create_nat_gateway(
            SubnetId=res.subnet_id,
            AllocationId=res.allocation_id,
            TagSpecifications=tag_spec("natgateway", res.node_id),
        )
        return str(resp["NatGateway"]["NatGatewayId"])

    @override
    def _after_create(self, client: Client, resource_id: str, res: NatGateway) -> None:
        # A gateway starts `pending` and may fail in the background (subnet or
        # EIP problems); returning early would report that as success. Waiting
        # here also covers an adopted gateway that is still pending. The waiter
        # raises on `failed`.
        client.get_waiter("nat_gateway_available").wait(
            NatGatewayIds=[resource_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )

    @override
    def _find(self, client: Client, res: NatGateway) -> str | None:
        return self._first_id(client, Filter=[{"Name": "subnet-id", "Values": [res.subnet_id]}])

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        client.delete_nat_gateway(NatGatewayId=resource_id)
        # NAT deletion takes minutes. Dependents deleted before it completes (the
        # EIP, subnet, VPC) fail with InUse/DependencyViolation.
        client.get_waiter("nat_gateway_deleted").wait(
            NatGatewayIds=[resource_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )


def _live_routes(live: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A described table's routes keyed by destination CIDR, minus the local route.

    The local route is created with the table and cannot be removed.
    """
    return {
        route["DestinationCidrBlock"]: route
        for route in live.get("Routes", [])
        if route.get("DestinationCidrBlock") is not None and route.get("GatewayId") != "local"
    }


class RouteTableHandler(Ec2Handler[RouteTable]):
    resource_type = RouteTable
    identity_field = "route_table_id"
    describe_call = "describe_route_tables"
    list_key = "RouteTables"
    id_key = "RouteTableId"
    ids_kwarg = "RouteTableIds"

    @override
    def _create(self, client: Client, res: RouteTable) -> str:
        resp = client.create_route_table(
            VpcId=res.vpc_id, TagSpecifications=tag_spec("route-table", res.node_id)
        )
        return str(resp["RouteTable"]["RouteTableId"])

    @override
    def _after_create(self, client: Client, resource_id: str, res: RouteTable) -> None:
        self._sync(client, resource_id, res)

    @override
    def update(self, client: Client, prior: dict[str, Any], res: RouteTable) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._sync(client, str(outputs[self.identity_field]), res)
        return outputs

    def _sync(self, client: Client, table_id: str, res: RouteTable) -> None:
        """Make the table's routes and associations match what config declares.

        A changed target uses ``replace_route``: a delete-then-add would leave a
        window with no default route.
        """
        live = client.describe_route_tables(RouteTableIds=[table_id])["RouteTables"][0]
        live_routes = _live_routes(live)
        declared = {route.cidr_block for route in res.routes}
        for cidr in [cidr for cidr in live_routes if cidr not in declared]:
            client.delete_route(RouteTableId=table_id, DestinationCidrBlock=cidr)
        for route in res.routes:
            self._put_route(client, table_id, route, live_routes.get(route.cidr_block))
        self._associate(client, table_id, res, live)

    @staticmethod
    def _put_route(
        client: Client, table_id: str, route: Route, existing: dict[str, Any] | None
    ) -> None:
        """Create ``route``, or repoint it when its live target differs.

        ``create_route`` cannot change an existing route's target: it raises
        ``RouteAlreadyExists``.
        """
        target: dict[str, Any] = (
            {"GatewayId": route.gateway_id}
            if route.gateway_id
            else {"NatGatewayId": route.nat_gateway_id}
        )
        if existing is None:
            client.create_route(
                RouteTableId=table_id, DestinationCidrBlock=route.cidr_block, **target
            )
        elif any(existing.get(key) != value for key, value in target.items()):
            client.replace_route(
                RouteTableId=table_id, DestinationCidrBlock=route.cidr_block, **target
            )

    @staticmethod
    def _associate(client: Client, table_id: str, res: RouteTable, live: dict[str, Any]) -> None:
        associated = {
            a["SubnetId"]: a["RouteTableAssociationId"]
            for a in live.get("Associations", [])
            if a.get("SubnetId")
        }
        for subnet_id in res.subnet_ids:
            if subnet_id not in associated:
                client.associate_route_table(RouteTableId=table_id, SubnetId=subnet_id)
        for subnet_id, association_id in associated.items():
            if subnet_id not in res.subnet_ids:
                client.disassociate_route_table(AssociationId=association_id)

    @override
    def _find(self, client: Client, res: RouteTable) -> str | None:
        # No attribute distinguishes a route table from others in the same VPC, so
        # the node tag is its only identity. An unmanaged table is found only by id.
        return self._find_tagged(client, res.node_id)

    @override
    def _delete(self, client: Client, resource_id: str) -> None:
        described = client.describe_route_tables(RouteTableIds=[resource_id])
        for association in described["RouteTables"][0].get("Associations", []):
            if association.get("SubnetId"):
                client.disassociate_route_table(
                    AssociationId=association["RouteTableAssociationId"]
                )
        client.delete_route_table(RouteTableId=resource_id)
