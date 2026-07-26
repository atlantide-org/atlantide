"""ACM certificate handler.

Pinned to ``us-east-1`` (CloudFront requires its viewer cert there) via a
``region()`` override rather than a resource field, so the stack's region does not
apply. The certificate is located by its ``arn``; on request ACM emits a DNS
validation record whose name/type/value are surfaced as computed outputs.
"""

from __future__ import annotations

import hashlib
from typing import Any

from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    ignore_missing,
    known_id,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import absent_ok, not_found
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import AcmCertificate


class AcmCertificateHandler(AwsHandler[AcmCertificate]):
    service = "acm"
    resource_type = AcmCertificate
    identity_field = "arn"

    @override
    def region(self, res: AcmCertificate) -> str:
        return Region.UsEast1  # CloudFront viewer certificates must live in us-east-1

    @override
    def create(self, client: Any, res: AcmCertificate) -> dict[str, Any]:
        # A certificate has no name to look it up by, so a retried create cannot
        # adopt the way the other handlers do; ACM's idempotency token returns the
        # same certificate instead. The provider's `_retrying` reissues this call
        # on transient failures.
        request: dict[str, Any] = {
            "DomainName": res.domain_name,
            "ValidationMethod": res.validation_method,
            "IdempotencyToken": _idempotency_token(res.node_id),
        }
        if res.subject_alternative_names:
            request["SubjectAlternativeNames"] = res.subject_alternative_names
        if res.tags:
            request["Tags"] = tag_list(res.tags)
        arn = client.request_certificate(**request)["CertificateArn"]
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    @override
    def read(self, client: Any, res: AcmCertificate) -> dict[str, Any] | None:
        arn = known_id(res, self.identity_field)
        if arn is None:
            return None
        if absent_ok(lambda: client.describe_certificate(CertificateArn=arn)) is None:
            return None
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    @override
    def update(self, client: Any, prior: dict[str, Any], res: AcmCertificate) -> dict[str, Any]:
        arn = prior.get(self.identity_field) or known_id(res, self.identity_field)
        if arn is None:  # update only runs on an existing (already-requested) cert
            raise not_found(res, "update")
        # ACM removes tags by whole object rather than by key, which is why the
        # untag callback is handed the live tags alongside the stale keys.
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
    def delete(self, client: Any, res: AcmCertificate) -> None:
        arn = known_id(res, self.identity_field)
        if arn is None:
            return
        with ignore_missing():
            client.delete_certificate(CertificateArn=arn)


def _idempotency_token(node_id: str) -> str:
    """A stable ACM idempotency token for one node.

    ACM allows 1-32 alphanumeric characters, so the node id — which carries colons
    and dots and is often longer — is hashed rather than passed through.
    """
    return hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:32]


def _validation_record(client: Any, arn: str, domain: str) -> dict[str, str]:
    """The DNS validation record ACM wants created, or blanks if not yet emitted.

    Real ACM populates ``ResourceRecord`` a moment after the request; a caller that
    needs it re-reads. Match the option by domain — order is not guaranteed.
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
