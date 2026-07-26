"""Route53 resources: a hosted zone and a record set.

Both are global (no ``region`` field). A zone is located by its provider-assigned
``zone_id``; a record has no id at all — its identity is ``(zone_id, record_name,
record_type)``, so every field that identifies it is immutable and any change to
one is a replace.
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

    An alias is how a zone apex reaches CloudFront or an ALB at all — those have
    no fixed IP, and DNS forbids a CNAME at the apex, so a plain record cannot
    express it.
    """

    #: The target's DNS name, e.g. a distribution's ``domain_name``.
    name: str
    #: The target's *hosted zone*, not the zone the record lives in. CloudFront's
    #: is the fixed :data:`CLOUDFRONT_ZONE_ID`; an ALB has its own.
    zone_id: str
    evaluate_target_health: bool = False


#: CloudFront's hosted zone, the same in every account and region. Named because
#: a magic string in a config is a thing nobody can check.
CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"


class Route53Record(AwsResource):
    """A record set in a hosted zone.

    Identity is ``(zone_id, record_name, record_type)`` — all immutable, so
    changing any of them replaces the record. ``ttl``, ``records`` and ``alias``
    update in place.

    Either ``records`` (with a ``ttl``) or ``alias``, never both: an alias has no
    TTL of its own, since it inherits the target's.
    """

    zone_id: str = immutable()  # a Ref to Route53HostedZone.zone_id, or a literal id
    record_name: str = immutable()
    record_type: str = immutable(default="A")
    ttl: int = mutable(default=300)
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
        return self
