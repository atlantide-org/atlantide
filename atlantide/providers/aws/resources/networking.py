"""EC2 networking resources: VPC, subnet, security group."""

from __future__ import annotations

from pydantic import Field, model_validator

from atlantide.core import Nested, computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import Ec2Resource

_CIDR = v.ipv4_cidr()


class Vpc(Ec2Resource):
    """An EC2 VPC. ``cidr_block``/``region`` immutable; ``tags`` in place; ``vpc_id`` computed."""

    cidr_block: str = immutable()
    vpc_id: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> Vpc:
        v.check(self.cidr_block, _CIDR)
        return self


class Subnet(Ec2Resource):
    """An EC2 subnet within a VPC.

    ``vpc_id`` (pass ``vpc.vpc_id``), ``cidr_block``, ``availability_zone`` and
    ``region`` are immutable; ``map_public_ip_on_launch`` and ``tags`` update in
    place. ``subnet_id`` is computed.

    ``availability_zone`` decides whether a deployment survives one zone failing,
    so it is worth stating: without it AWS picks, and two subnets meant to be in
    different zones can land in the same one. Pass a name from
    :class:`~atlantide.providers.aws.resources.data.AwsAvailabilityZones` rather
    than a literal — which letters exist differs per account as well as region.
    """

    vpc_id: str = immutable()
    cidr_block: str = immutable()
    availability_zone: str | None = immutable(default=None)
    #: Give instances launched here a public IP. Off by default: a subnet that
    #: hands out public addresses should be one somebody chose.
    map_public_ip_on_launch: bool = mutable(default=False)
    subnet_id: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> Subnet:
        v.check(self.cidr_block, _CIDR)
        return self


class SgRule(Nested):
    """One security-group rule.

    ``protocol="-1"`` means every protocol, in which case the ports are ignored —
    AWS's spelling, kept rather than invented so a rule reads the same here as in
    the console.

    Exactly one source is given: ``cidr_blocks``/``ipv6_cidr_blocks`` for
    addresses, or ``source_security_group_id`` for another group. Passing that a
    ``Ref`` (``other.group_id``) is what makes group-to-group access a real
    dependency edge rather than a string nobody ordered.
    """

    protocol: str = "tcp"
    from_port: int | None = None
    to_port: int | None = None
    cidr_blocks: list[str] = Field(default_factory=list)
    ipv6_cidr_blocks: list[str] = Field(default_factory=list)
    source_security_group_id: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _validate(self) -> SgRule:
        if self.protocol != "-1" and (self.from_port is None or self.to_port is None):
            raise ValueError(
                f"SgRule for protocol {self.protocol!r} needs from_port and to_port "
                f"(use protocol='-1' for all traffic)"
            )
        return self


#: AWS's implicit outbound rule on a new group. Named so a config can say
#: "everything except this" without spelling out the shape.
#: Mirrors the egress rule AWS attaches to a new security group — including its
#: *empty* description. A friendlier string here would never reach AWS (the rule
#: is created by AWS, not by us), so every read would report it as drift and every
#: untouched security group would look permanently out of sync.
ALLOW_ALL_EGRESS = SgRule(protocol="-1", cidr_blocks=["0.0.0.0/0"])


class SecurityGroup(Ec2Resource):
    """An EC2 security group within a VPC.

    ``group_name``, ``description``, ``vpc_id`` (pass ``vpc.vpc_id``) and
    ``region`` are immutable (AWS forbids editing name/description/VPC);
    ``ingress``, ``egress`` and ``tags`` update in place. ``group_id`` is computed.

    **Egress defaults to open, as AWS does.** A new group is created with an
    allow-all egress rule, and this mirrors that rather than quietly diverging: a
    group whose outbound traffic silently stopped working would be a worse
    surprise than one that matches the console. Pass ``egress=[]`` to mean *no*
    outbound access — the allow-all rule is then revoked explicitly.
    """

    group_name: str = immutable(physical_name=True)
    description: str = immutable(default="managed by atlantide")
    vpc_id: str = immutable()
    ingress: list[SgRule] = mutable(default_factory=list)
    egress: list[SgRule] = mutable(default_factory=lambda: [ALLOW_ALL_EGRESS])
    group_id: str = computed()


class InternetGateway(Ec2Resource):
    """An internet gateway, attached to ``vpc_id``.

    The attachment is a field rather than a seventh resource type: an unattached
    gateway does nothing, and a config that could express one would only be able
    to express a mistake.
    """

    vpc_id: str = immutable()
    internet_gateway_id: str = computed()


class ElasticIp(Ec2Resource):
    """A static public address, for a NAT gateway to sit behind."""

    allocation_id: str = computed()
    public_ip: str = computed()


class NatGateway(Ec2Resource):
    """A NAT gateway: outbound internet for a private subnet.

    ``subnet_id`` must be a *public* subnet — one whose route table sends
    ``0.0.0.0/0`` to an internet gateway. A NAT in a private subnet is the classic
    way to build a VPC that looks right and routes nowhere.
    """

    subnet_id: str = immutable()
    allocation_id: str = immutable()
    nat_gateway_id: str = computed()


class Route(Nested):
    """One entry in a route table: a destination and where to send it."""

    cidr_block: str = "0.0.0.0/0"
    gateway_id: str | None = None
    nat_gateway_id: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Route:
        targets = [self.gateway_id, self.nat_gateway_id]
        if sum(target is not None for target in targets) != 1:
            raise ValueError("Route needs exactly one of gateway_id or nat_gateway_id")
        return self


class RouteTable(Ec2Resource):
    """A route table and the subnets that use it.

    Routes are inline rather than a resource each: their order is irrelevant,
    they have no identity of their own, and one-resource-per-route turns a
    three-line table into three nodes whose only purpose is to be counted.

    ``subnet_ids`` associates the table; a subnet may belong to one table, so the
    association lives here rather than being a third thing to keep in step.
    """

    vpc_id: str = immutable()
    routes: list[Route] = mutable(default_factory=list)
    subnet_ids: list[str] = mutable(default_factory=list)
    route_table_id: str = computed()
