"""ACM certificate with DNS validation.

Global to the config but the handler pins the client to ``us-east-1`` — CloudFront
requires its viewer certificate to live there. The certificate is located by its
``arn``. On request, ACM emits a DNS validation record (a CNAME); its name/type/
value are surfaced as computed outputs so a ``Route53Record`` can create it and the
certificate can validate.
"""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_VALIDATION_METHOD = v.one_of(("DNS", "EMAIL"), "ACM validation method")


class AcmCertificate(AwsResource):
    """An ACM certificate (DNS validation by default).

    ``domain_name``, ``subject_alternative_names`` and ``validation_method`` are
    immutable; ``tags`` update in place. The ``validation_*`` outputs carry the DNS
    record ACM wants created to prove domain ownership.
    """

    domain_name: str = immutable(physical_name=True)
    subject_alternative_names: list[str] = immutable(default_factory=list)
    validation_method: str = immutable(default="DNS")
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()  # CertificateArn (the id)
    validation_name: str = computed()  # the DNS validation record name
    validation_type: str = computed()  # ...its type (CNAME)
    validation_value: str = computed()  # ...its value

    @model_validator(mode="after")
    def _validate(self) -> AcmCertificate:
        v.check(self.validation_method, _VALIDATION_METHOD)
        return self
