"""ACM certificate with DNS validation.

Has no ``region`` field: the handler pins the client to ``us-east-1``, where
CloudFront requires its viewer certificate. The certificate is located by its
``arn``. ACM emits a DNS validation record (a CNAME), exposed as computed outputs so
a ``Route53Record`` can create it.
"""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import TaggedResource

_VALIDATION_METHOD = v.one_of(("DNS", "EMAIL"), "ACM validation method")
_DOMAIN = v.domain_name("certificate domain")


class AcmCertificate(TaggedResource):
    """An ACM certificate (DNS validation by default).

    ``domain_name``, ``subject_alternative_names`` and ``validation_method`` are
    immutable; ``tags`` update in place. The ``validation_*`` outputs carry the DNS
    record ACM wants created to prove domain ownership.
    """

    domain_name: str = immutable(physical_name=True)
    subject_alternative_names: list[str] = immutable(default_factory=list)
    validation_method: str = immutable(default="DNS")
    arn: str = computed()  # CertificateArn (the id)
    validation_name: str = computed()
    validation_type: str = computed()  # CNAME
    validation_value: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> AcmCertificate:
        v.check(self.validation_method, _VALIDATION_METHOD)
        v.check(self.domain_name, _DOMAIN)
        for alternative in self.subject_alternative_names:
            v.check(alternative, _DOMAIN)
        return self
