"""ACM certificate handler.

Pinned to ``us-east-1`` (CloudFront requires its viewer cert there) via a
``region()`` override rather than a resource field, so the stack's region does not
apply. The certificate is located by its ``arn``; on request ACM emits a DNS
validation record whose name/type/value are surfaced as computed outputs.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, override

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    ignore_missing,
    known_id,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import absent_ok, not_found
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import AcmCertificate

#: Bounded poll for ACM to emit the DNS validation record after a request; it
#: usually appears within a few seconds.
_RECORD_POLL_ATTEMPTS = 30
_RECORD_POLL_DELAY = 2.0


class AcmCertificateHandler(AwsHandler[AcmCertificate]):
    service = "acm"
    resource_type = AcmCertificate
    identity_field = "arn"

    @override
    def region(self, res: AcmCertificate) -> str:
        return Region.UsEast1

    @override
    def create(self, client: Client, res: AcmCertificate) -> dict[str, Any]:
        # A certificate has no name to look up, so a retried create cannot adopt;
        # ACM's idempotency token returns the same certificate instead. The
        # provider's `_retrying` reissues this call on transient failures.
        request: dict[str, Any] = {
            "DomainName": res.domain_name,
            "ValidationMethod": res.validation_method,
            "IdempotencyToken": _idempotency_token(res),
        }
        if res.subject_alternative_names:
            request["SubjectAlternativeNames"] = res.subject_alternative_names
        if res.tags:
            request["Tags"] = tag_list(res.tags)
        arn = client.request_certificate(**request)["CertificateArn"]
        if res.validation_method == "DNS":
            return {"arn": arn, **_await_validation_record(client, arn, res)}
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    @override
    def read(self, client: Client, res: AcmCertificate) -> dict[str, Any] | None:
        arn = known_id(res, self.identity_field)
        if arn is None:
            return None
        if absent_ok(lambda: client.describe_certificate(CertificateArn=arn)) is None:
            return None
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    @override
    def update(self, client: Client, prior: dict[str, Any], res: AcmCertificate) -> dict[str, Any]:
        arn = prior.get(self.identity_field) or known_id(res, self.identity_field)
        if arn is None:  # update only runs on an existing (already-requested) cert
            raise not_found(res, "update")
        # ACM removes tags by key and value, so the untag callback builds the tag
        # objects from the live tags.
        sync_tags(
            res.tags,
            live=lambda: tags_from_list(
                client.list_tags_for_certificate(CertificateArn=arn).get("Tags", [])
            ),
            untag=lambda stale, live: client.remove_tags_from_certificate(
                CertificateArn=arn, Tags=[{"Key": key, "Value": live[key]} for key in stale]
            ),
            tag=lambda tags: client.add_tags_to_certificate(
                CertificateArn=arn, Tags=tag_list(tags)
            ),
        )
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    @override
    def delete(self, client: Client, res: AcmCertificate) -> None:
        arn = known_id(res, self.identity_field)
        if arn is None:
            return
        with ignore_missing():
            client.delete_certificate(CertificateArn=arn)


def _idempotency_token(res: AcmCertificate) -> str:
    """A stable ACM idempotency token for one node and its immutable request.

    ACM returns the earlier certificate for a repeated token within an hour, so the
    token covers every field that forces a replacement: a replaced certificate with
    a new domain must not get the old one back. ACM allows 1-32 alphanumeric
    characters; the node id contains colons and dots and can be longer, so it is
    hashed.
    """
    key = "\n".join(
        [
            res.node_id,
            res.domain_name,
            ",".join(sorted(res.subject_alternative_names)),
            res.validation_method,
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _await_validation_record(client: Client, arn: str, res: AcmCertificate) -> dict[str, str]:
    """The DNS validation record, polling until ACM emits it.

    ACM fills ``ResourceRecord`` a few seconds after the request; stored blank,
    the ``validation_*`` outputs would give a dependent ``Route53Record`` nothing
    to create.
    """
    for attempt in range(_RECORD_POLL_ATTEMPTS):
        record = _validation_record(client, arn, res.domain_name)
        if record["validation_name"]:
            return record
        if attempt + 1 < _RECORD_POLL_ATTEMPTS:
            time.sleep(_RECORD_POLL_DELAY)
    raise ProviderError(
        f"ACM did not emit the DNS validation record for {arn} after "
        f"{_RECORD_POLL_ATTEMPTS * _RECORD_POLL_DELAY:.0f}s",
        op="create",
        resource_type=res.type_name(),
    )


def _validation_record(client: Client, arn: str, domain: str) -> dict[str, str]:
    """The DNS validation record ACM wants created, or blanks if not yet emitted.

    ACM populates ``ResourceRecord`` shortly after the request; a caller that needs
    it re-reads. Options are matched by domain because their order is not
    guaranteed.
    """
    options = client.describe_certificate(CertificateArn=arn)["Certificate"].get(
        "DomainValidationOptions", []
    )
    option = next((o for o in options if o.get("DomainName") == domain), None)
    record = (option or (options[0] if options else {})).get("ResourceRecord") or {}
    return {
        "validation_name": record.get("Name", ""),
        "validation_type": record.get("Type", ""),
        "validation_value": record.get("Value", ""),
    }
