"""EC2 networking resources: VPC, subnet, security group."""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable
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

    ``vpc_id`` (pass ``vpc.vpc_id``), ``cidr_block`` and ``region`` are immutable;
    ``tags`` update in place. ``subnet_id`` is computed.
    """

    vpc_id: str = immutable()
    cidr_block: str = immutable()
    subnet_id: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> Subnet:
        v.check(self.cidr_block, _CIDR)
        return self


class SecurityGroup(Ec2Resource):
    """An EC2 security group within a VPC.

    ``group_name``, ``description``, ``vpc_id`` (pass ``vpc.vpc_id``) and
    ``region`` are immutable (AWS forbids editing name/description/VPC); ``tags``
    update in place. ``group_id`` is computed.
    """

    group_name: str = immutable(physical_name=True)
    description: str = immutable(default="managed by atlantide")
    vpc_id: str = immutable()
    group_id: str = computed()
