"""Route53 resources: a hosted zone and a record set.

Both are global (no ``region`` field). A zone is located by its provider-assigned
``zone_id``; a record has no id at all — its identity is ``(zone_id, record_name,
record_type)``, so every field that identifies it is immutable and any change to
one is a replace.
"""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_RECORD_TYPE = v.one_of(("A", "AAAA", "CNAME", "TXT", "MX", "NS"), "DNS record type")


class Route53HostedZone(AwsResource):
    """A Route53 public hosted zone for ``domain``.

    ``domain`` is immutable; ``comment`` updates in place. ``name_servers`` are the
    delegation-set servers to point the registrar at.
    """

    domain: str = immutable(physical_name=True)
    comment: str = mutable(default="")
    zone_id: str = computed()  # HostedZone.Id (sans /hostedzone/ prefix)
    name_servers: list[str] = computed()  # DelegationSet.NameServers


class Route53Record(AwsResource):
    """A record set in a hosted zone.

    Identity is ``(zone_id, record_name, record_type)`` — all immutable, so
    changing any of them replaces the record. ``ttl`` and ``records`` (the rdata
    values) update in place.
    """

    zone_id: str = immutable()  # a Ref to Route53HostedZone.zone_id, or a literal id
    record_name: str = immutable()
    record_type: str = immutable(default="A")
    ttl: int = mutable(default=300)
    records: list[str] = mutable(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> Route53Record:
        v.check(self.record_type, _RECORD_TYPE)
        return self
