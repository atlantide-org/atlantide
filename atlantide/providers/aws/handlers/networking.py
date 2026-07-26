"""EC2 networking handlers: VPCs, subnets, security groups, and routing.

The EC2 base these share lives in :mod:`.ec2`; the security-group rule
translation in :mod:`.sgrules`.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import override

from atlantide.providers.aws.handlers.ec2 import (
    Ec2Handler,
    tag_spec,
)
from atlantide.providers.aws.handlers.sgrules import (
    atomic_units,
    has_rule,
    rule_to_aws,
    rules_from_aws,
)
from atlantide.providers.aws.resources import SecurityGroup, Subnet, Vpc
from atlantide.providers.aws.resources.networking import (
    ElasticIp,
    InternetGateway,
    NatGateway,
    RouteTable,
)


class VpcHandler(Ec2Handler[Vpc]):
    resource_type = Vpc
    identity_field = "vpc_id"
    describe_call = "describe_vpcs"
    list_key = "Vpcs"
    id_key = "VpcId"
    ids_kwarg = "VpcIds"

    @override
    def _create(self, client: Any, res: Vpc) -> str:
        resp = client.create_vpc(
            CidrBlock=res.cidr_block, TagSpecifications=tag_spec("vpc", res.node_id)
        )
        return str(resp["Vpc"]["VpcId"])

    @override
    def _find(self, client: Any, res: Vpc) -> str | None:
        return self._first_id(client, Filters=[{"Name": "cidr", "Values": [res.cidr_block]}])

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_vpc(VpcId=resource_id)


class SubnetHandler(Ec2Handler[Subnet]):
    resource_type = Subnet
    identity_field = "subnet_id"
    describe_call = "describe_subnets"
    list_key = "Subnets"
    id_key = "SubnetId"
    ids_kwarg = "SubnetIds"

    @override
    def _create(self, client: Any, res: Subnet) -> str:
        kwargs: dict[str, Any] = {
            "VpcId": res.vpc_id,
            "CidrBlock": res.cidr_block,
            "TagSpecifications": tag_spec("subnet", res.node_id),
        }
        if res.availability_zone is not None:
            kwargs["AvailabilityZone"] = res.availability_zone
        return str(client.create_subnet(**kwargs)["Subnet"]["SubnetId"])

    @override
    def _after_create(self, client: Any, resource_id: str, res: Subnet) -> None:
        self._set_public_ip(client, resource_id, res)

    @override
    def update(self, client: Any, prior: dict[str, Any], res: Subnet) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._set_public_ip(client, str(outputs[self.identity_field]), res)
        return outputs

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        # Only what the describe actually returned: a key AWS omitted is unchecked,
        # not equal to whatever config asked for.
        return {
            name: caster(live[key])
            for name, key, caster in (
                ("availability_zone", "AvailabilityZone", str),
                ("map_public_ip_on_launch", "MapPublicIpOnLaunch", bool),
            )
            if key in live
        }

    @staticmethod
    def _set_public_ip(client: Any, subnet_id: str, res: Subnet) -> None:
        client.modify_subnet_attribute(
            SubnetId=subnet_id,
            MapPublicIpOnLaunch={"Value": res.map_public_ip_on_launch},
        )

    @override
    def _find(self, client: Any, res: Subnet) -> str | None:
        return self._first_id(
            client,
            Filters=[
                {"Name": "cidr-block", "Values": [res.cidr_block]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ],
        )

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_subnet(SubnetId=resource_id)


class SecurityGroupHandler(Ec2Handler[SecurityGroup]):
    resource_type = SecurityGroup
    identity_field = "group_id"
    describe_call = "describe_security_groups"
    list_key = "SecurityGroups"
    id_key = "GroupId"
    ids_kwarg = "GroupIds"

    @override
    def _create(self, client: Any, res: SecurityGroup) -> str:
        resp = client.create_security_group(
            GroupName=res.group_name,
            Description=res.description,
            VpcId=res.vpc_id,
            TagSpecifications=tag_spec("security-group", res.node_id),
        )
        return str(resp["GroupId"])

    @override
    def _after_create(self, client: Any, resource_id: str, res: SecurityGroup) -> None:
        self._sync_rules(client, resource_id, res)

    @override
    def update(self, client: Any, prior: dict[str, Any], res: SecurityGroup) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._sync_rules(client, str(outputs[self.identity_field]), res)
        return outputs

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        # Report the rules, so a port opened in the console shows as drift rather
        # than as a blanket "in sync" nothing checked. An absent key is unchecked;
        # an empty list is the meaningful "no rules".
        return {
            name: rules_from_aws(live[key])
            for name, key in (("ingress", "IpPermissions"), ("egress", "IpPermissionsEgress"))
            if key in live
        }

    def _sync_rules(self, client: Any, group_id: str, res: SecurityGroup) -> None:
        """Make the live rules match the declared ones, in both directions.

        Rules are set-like: AWS has no "replace the rule set" call, so the delta
        is computed here and applied as an authorize plus a revoke. Revoking is
        the half that matters — without it, removing a rule from config would
        leave the port open, which is the failure mode nobody notices.

        A new group is born with an allow-all egress rule. Declaring ``egress=[]``
        therefore has to *revoke* something rather than simply not add it.
        """
        live = client.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
        for direction, key, authorize, revoke in (
            (
                res.ingress,
                "IpPermissions",
                client.authorize_security_group_ingress,
                client.revoke_security_group_ingress,
            ),
            (
                res.egress,
                "IpPermissionsEgress",
                client.authorize_security_group_egress,
                client.revoke_security_group_egress,
            ),
        ):
            # Compared and applied per atomic unit (one range each): EC2 merges
            # live ranges into one permission per (protocol, from, to), so whole-
            # permission comparison re-authorizes present ranges and revokes
            # merged rules wholesale. See :func:`atomic_units`.
            desired = atomic_units([rule_to_aws(rule) for rule in direction])
            current = atomic_units(live.get(key, []))
            if added := [p for p in desired if not has_rule(current, p)]:
                authorize(GroupId=group_id, IpPermissions=added)
            if stale := [p for p in current if not has_rule(desired, p)]:
                revoke(GroupId=group_id, IpPermissions=stale)

    @override
    def _find(self, client: Any, res: SecurityGroup) -> str | None:
        return self._first_id(
            client,
            Filters=[
                {"Name": "group-name", "Values": [res.group_name]},
                {"Name": "vpc-id", "Values": [res.vpc_id]},
            ],
        )

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_security_group(GroupId=resource_id)


class InternetGatewayHandler(Ec2Handler[InternetGateway]):
    resource_type = InternetGateway
    identity_field = "internet_gateway_id"
    describe_call = "describe_internet_gateways"
    list_key = "InternetGateways"
    id_key = "InternetGatewayId"
    ids_kwarg = "InternetGatewayIds"

    @override
    def _create(self, client: Any, res: InternetGateway) -> str:
        resp = client.create_internet_gateway(
            TagSpecifications=tag_spec("internet-gateway", res.node_id)
        )
        return str(resp["InternetGateway"]["InternetGatewayId"])

    @override
    def _after_create(self, client: Any, resource_id: str, res: InternetGateway) -> None:
        # Idempotent attach: an adopted gateway (re-run create) is already attached.
        described = client.describe_internet_gateways(InternetGatewayIds=[resource_id])
        attachments = described["InternetGateways"][0].get("Attachments", [])
        if not any(a.get("VpcId") == res.vpc_id for a in attachments):
            client.attach_internet_gateway(InternetGatewayId=resource_id, VpcId=res.vpc_id)

    @override
    def _find(self, client: Any, res: InternetGateway) -> str | None:
        return self._first_id(
            client, Filters=[{"Name": "attachment.vpc-id", "Values": [res.vpc_id]}]
        )

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        # Detach first: AWS refuses to delete an attached gateway, and the VPC it
        # is attached to is read back rather than taken from config, which by
        # this point may no longer describe it.
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
    def _create(self, client: Any, res: ElasticIp) -> str:
        resp = client.allocate_address(
            Domain="vpc", TagSpecifications=tag_spec("elastic-ip", res.node_id)
        )
        return str(resp["AllocationId"])

    @override
    def create(self, client: Any, res: ElasticIp) -> dict[str, Any]:
        outputs = super().create(client, res)
        return {**outputs, **self._address(client, str(outputs[self.identity_field]))}

    @override
    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        return {"public_ip": live["PublicIp"]} if "PublicIp" in live else {}

    @staticmethod
    def _address(client: Any, allocation_id: str) -> dict[str, Any]:
        found = client.describe_addresses(AllocationIds=[allocation_id])["Addresses"]
        return {"public_ip": found[0].get("PublicIp", "")} if found else {}

    @override
    def _find(self, client: Any, res: ElasticIp) -> str | None:
        # An address has no attributes of its own to match on, so the node tag is
        # the only identity — `_find` and `_find_tagged` are the same lookup. That
        # also means an address atlantide did not create cannot be found this way;
        # `read` prefers the id from state precisely so it does not have to be.
        return self._find_tagged(client, res.node_id)

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        client.release_address(AllocationId=resource_id)


#: A deleted NAT lingers in the API for a while, so its id still resolves and a
#: filter still matches it. Treating one as live would make a destroyed gateway
#: read as present — enforced once, in `_is_live`, which every lookup consults.
_DEAD_NAT_STATES = frozenset({"deleted", "deleting"})


class NatGatewayHandler(Ec2Handler[NatGateway]):
    resource_type = NatGateway
    identity_field = "nat_gateway_id"
    describe_call = "describe_nat_gateways"
    list_key = "NatGateways"
    id_key = "NatGatewayId"
    ids_kwarg = "NatGatewayIds"
    filters_kwarg = "Filter"  # EC2's one singular spelling

    @staticmethod
    @override
    def _is_live(item: dict[str, Any]) -> bool:
        return item.get("State") not in _DEAD_NAT_STATES

    @override
    def _create(self, client: Any, res: NatGateway) -> str:
        resp = client.create_nat_gateway(
            SubnetId=res.subnet_id,
            AllocationId=res.allocation_id,
            TagSpecifications=tag_spec("natgateway", res.node_id),
        )
        return str(resp["NatGateway"]["NatGatewayId"])

    @override
    def _find(self, client: Any, res: NatGateway) -> str | None:
        return self._first_id(client, Filter=[{"Name": "subnet-id", "Values": [res.subnet_id]}])

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        client.delete_nat_gateway(NatGatewayId=resource_id)
        # NAT deletion takes minutes, and the dependents run right after this
        # returns: releasing the EIP (still associated) and deleting the subnet/
        # VPC fail with InUse/DependencyViolation — a routine full destroy broke
        # partway every time. Wait for the gateway to actually be gone.
        client.get_waiter("nat_gateway_deleted").wait(
            NatGatewayIds=[resource_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )


class RouteTableHandler(Ec2Handler[RouteTable]):
    resource_type = RouteTable
    identity_field = "route_table_id"
    describe_call = "describe_route_tables"
    list_key = "RouteTables"
    id_key = "RouteTableId"
    ids_kwarg = "RouteTableIds"

    @override
    def _create(self, client: Any, res: RouteTable) -> str:
        resp = client.create_route_table(
            VpcId=res.vpc_id, TagSpecifications=tag_spec("route-table", res.node_id)
        )
        return str(resp["RouteTable"]["RouteTableId"])

    @override
    def _after_create(self, client: Any, resource_id: str, res: RouteTable) -> None:
        self._sync(client, resource_id, res)

    @override
    def update(self, client: Any, prior: dict[str, Any], res: RouteTable) -> dict[str, Any]:
        outputs = super().update(client, prior, res)
        self._sync(client, str(outputs[self.identity_field]), res)
        return outputs

    def _sync(self, client: Any, table_id: str, res: RouteTable) -> None:
        """Make the table's routes and associations match what config declares.

        Routes are replaced rather than diffed: a route table holds a handful of
        entries, ``replace_route`` exists for exactly this, and a delete-then-add
        would leave a window with no default route at all.
        """
        live = client.describe_route_tables(RouteTableIds=[table_id])["RouteTables"][0]
        declared = {route.cidr_block: route for route in res.routes}
        live_routes: dict[str, dict[str, Any]] = {}
        for existing in live.get("Routes", []):
            cidr = existing.get("DestinationCidrBlock")
            # The local route is created with the table and cannot be removed.
            if cidr is None or existing.get("GatewayId") == "local":
                continue
            live_routes[cidr] = existing
            if cidr not in declared:
                client.delete_route(RouteTableId=table_id, DestinationCidrBlock=cidr)
        for route in res.routes:
            target: dict[str, Any] = (
                {"GatewayId": route.gateway_id}
                if route.gateway_id
                else {"NatGatewayId": route.nat_gateway_id}
            )
            existing = live_routes.get(route.cidr_block)
            if existing is None:
                client.create_route(
                    RouteTableId=table_id,
                    DestinationCidrBlock=route.cidr_block,
                    **target,
                )
            elif any(existing.get(key) != value for key, value in target.items()):
                # An existing route's target changed. `create_route` cannot do
                # this — it raises RouteAlreadyExists — and swallowing that made
                # repointing a route (IGW -> NAT) a silent no-op forever.
                client.replace_route(
                    RouteTableId=table_id,
                    DestinationCidrBlock=route.cidr_block,
                    **target,
                )
        self._associate(client, table_id, res, live)

    @staticmethod
    def _associate(client: Any, table_id: str, res: RouteTable, live: dict[str, Any]) -> None:
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
    def _find(self, client: Any, res: RouteTable) -> str | None:
        # Nothing on a route table distinguishes it from its siblings in the same
        # VPC, so the node tag is the only attribute identity there is. As with an
        # address, that makes an unmanaged table findable only by its id.
        return self._find_tagged(client, res.node_id)

    @override
    def _delete(self, client: Any, resource_id: str) -> None:
        described = client.describe_route_tables(RouteTableIds=[resource_id])
        for association in described["RouteTables"][0].get("Associations", []):
            if association.get("SubnetId"):
                client.disassociate_route_table(
                    AssociationId=association["RouteTableAssociationId"]
                )
        client.delete_route_table(RouteTableId=resource_id)
