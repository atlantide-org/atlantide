"""Route53 resources: a hosted zone and a record set.

Both are global (no ``region`` field). A zone is located by its provider-assigned
``zone_id``; a record has no id and is identified by its zone, name and type.
"""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import Nested, computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_RECORD_TYPE = v.one_of(("A", "AAAA", "CNAME", "TXT", "MX", "NS"), "DNS record type")
_DOMAIN = v.domain_name("hosted zone domain")


class Route53HostedZone(AwsResource):
    """A Route53 public hosted zone for ``domain``.

    ``domain`` is immutable; ``comment`` updates in place. ``name_servers`` are the
    delegation-set servers to point the registrar at.
    """

    domain: str = immutable(physical_name=True)
    comment: str = mutable(default="")
    zone_id: str = computed()  # HostedZone.Id (sans /hostedzone/ prefix)
    name_servers: list[str] = computed()  # DelegationSet.NameServers

    @model_validator(mode="after")
    def _validate(self) -> Route53HostedZone:
        v.check(self.domain, _DOMAIN)
        return self


class AliasTarget(Nested):
    """Where an alias record points: another AWS resource, not an address.

    A zone apex needs an alias to reach CloudFront or an ALB: those have no fixed
    IP, and DNS forbids a CNAME at the apex.
    """

    #: The target's DNS name, e.g. a distribution's ``domain_name``.
    name: str
    #: The target's *hosted zone*, not the zone the record lives in. CloudFront's
    #: is the fixed :data:`CLOUDFRONT_ZONE_ID`; an ALB has its own.
    zone_id: str
    evaluate_target_health: bool = False


#: CloudFront's hosted zone, the same in every account and region.
CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"

#: ``Route53Record.ttl`` when unset; the only value an alias record accepts.
_DEFAULT_TTL = 300


class Route53Record(AwsResource):
    """A record set in a hosted zone.

    Identity is ``(zone_id, record_name, record_type)``, all immutable: changing
    any of them replaces the record. ``ttl``, ``records`` and ``alias``
    update in place.

    Exactly one of ``records`` (with a ``ttl``) or ``alias``: an alias has no TTL
    of its own (it inherits the target's), so a non-default ``ttl`` with an alias
    is rejected rather than silently ignored.
    """

    zone_id: str = immutable()  # a Ref to Route53HostedZone.zone_id, or a literal id
    record_name: str = immutable()
    record_type: str = immutable(default="A")
    ttl: int = mutable(default=_DEFAULT_TTL)
    records: list[str] = mutable(default_factory=list)
    alias: AliasTarget | None = mutable(default=None)

    @model_validator(mode="after")
    def _validate(self) -> Route53Record:
        v.check(self.record_type, _RECORD_TYPE)
        if self.alias is not None and self.records:
            raise ValueError(
                "Route53Record takes either records or alias, not both — an alias "
                "record has no rdata of its own"
            )
        if self.alias is None and not self.records:
            raise ValueError("Route53Record needs either records or alias")
        if self.alias is not None and self.ttl != _DEFAULT_TTL:
            raise ValueError(
                "Route53Record with an alias takes no ttl — an alias record inherits "
                "its target's TTL"
            )
        return self
