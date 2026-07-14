"""ACM certificate handler.

Pinned to ``us-east-1`` (CloudFront requires its viewer cert there) via a
``region()`` override rather than a resource field, so the stack's region does not
apply. The certificate is located by its ``arn``; on request ACM emits a DNS
validation record whose name/type/value are surfaced as computed outputs.
"""

from __future__ import annotations

from typing import Any

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, known_id, tag_list
from atlantide.providers.aws.region import Region
from atlantide.providers.aws.resources import AcmCertificate


class AcmCertificateHandler(AwsHandler[AcmCertificate]):
    service = "acm"
    resource_type = AcmCertificate

    def region(self, res: AcmCertificate) -> str:
        return Region.UsEast1  # CloudFront viewer certificates must live in us-east-1

    def create(self, client: Any, res: AcmCertificate) -> dict[str, Any]:
        request: dict[str, Any] = {
            "DomainName": res.domain_name,
            "ValidationMethod": res.validation_method,
        }
        if res.subject_alternative_names:
            request["SubjectAlternativeNames"] = res.subject_alternative_names
        if res.tags:
            request["Tags"] = tag_list(res.tags)
        arn = client.request_certificate(**request)["CertificateArn"]
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    def read(self, client: Any, res: AcmCertificate) -> dict[str, Any] | None:
        arn = known_id(res, "arn")
        if arn is None:
            return None
        try:
            client.describe_certificate(CertificateArn=arn)
        except client.exceptions.ClientError:
            return None
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    def update(self, client: Any, prior: dict[str, Any], res: AcmCertificate) -> dict[str, Any]:
        arn = prior.get("arn") or known_id(res, "arn")
        if arn is None:  # update only runs on an existing (already-requested) cert
            raise ProviderError(
                "AcmCertificate not found", op="update", resource_type=res.type_name()
            )
        if res.tags:
            client.add_tags_to_certificate(CertificateArn=arn, Tags=tag_list(res.tags))
        return {"arn": arn, **_validation_record(client, arn, res.domain_name)}

    def delete(self, client: Any, res: AcmCertificate) -> None:
        arn = known_id(res, "arn")
        if arn is None:
            return
        with ignore_missing():
            client.delete_certificate(CertificateArn=arn)


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
