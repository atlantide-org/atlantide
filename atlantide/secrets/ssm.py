"""Remote secrets backend: AWS SSM Parameter Store.

A ``SecretRef("db_password", provider="ssm")`` resolves to the decrypted value of
the parameter at ``{prefix}db_password``. Like every other
:class:`~atlantide.secrets.base.SecretsProvider`, the value is fetched
in memory at apply time and never written to config, the IR, or state; state
keeps only the rotation digest.

Values are memoised per instance, so a config referencing one secret from several
resources costs one API call and yields one consistent value for the whole run.
"""

from __future__ import annotations

from typing import Any, ClassVar, override

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets.base import SecretsProvider
from atlantide.util.aws import client_config, error_code

#: Bounded, retried client config. Resolution happens mid-apply while the run
#: holds its lease, so an SSM call must not hang.
_CLIENT_CONFIG = client_config()

#: Error codes meaning "the store answered; the name is not in it".
_NOT_FOUND = frozenset({"ParameterNotFound"})

#: Error codes meaning "the store answered and refused access".
_DENIED = frozenset({"AccessDeniedException", "AccessDenied"})

#: A name no parameter store should hold, used to prove one answers at all.
_PROBE = "atlantide-preflight-probe"


class SsmParameterStore(SecretsProvider):
    """Resolves a secret name to an SSM parameter value (``WithDecryption``)."""

    name: ClassVar[str] = "ssm"

    def __init__(
        self,
        *,
        prefix: str = "",
        region: str | None = None,
        profile: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._prefix = prefix
        session = boto3.Session(profile_name=profile, region_name=region)
        # boto3-stubs overloads client() per literal service name, so go through
        # an untyped factory.
        make_client: Any = session.client
        self._client: Any = make_client("ssm", endpoint_url=endpoint_url, config=_CLIENT_CONFIG)
        self._cache: dict[str, str] = {}

    @override
    def resolve(self, name: str) -> str:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        path = f"{self._prefix}{name}"
        try:
            response = self._client.get_parameter(Name=path, WithDecryption=True)
        except ClientError as exc:
            raise self._error(exc, name, path) from exc
        except BotoCoreError as exc:  # no credentials, bad profile, no endpoint, timeout
            raise SecretsError(f"cannot reach SSM for {path!r}: {exc}") from exc
        parameter = response["Parameter"]
        if parameter.get("Type") == "StringList":
            raise SecretsError(
                f"secret {name!r} maps to SSM parameter {path!r} of type StringList; "
                f"only String and SecureString hold a single secret value"
            )
        value = str(parameter["Value"])
        self._cache[name] = value
        return value

    def _error(self, exc: ClientError, name: str, path: str) -> SecretsError:
        code = error_code(exc)
        if code in _NOT_FOUND:
            return SecretsError(
                f"secret {name!r} not found in SSM at {path!r} — "
                f"create it with `aws ssm put-parameter --name {path} "
                f"--type SecureString --value ...`"
            )
        if code in _DENIED:
            return SecretsError(
                f"access denied reading SSM parameter {path!r} — the caller needs "
                f"ssm:GetParameter (and kms:Decrypt for a SecureString)"
            )
        return SecretsError(f"cannot read SSM parameter {path!r}: {exc}")

    @override
    def check(self) -> Check:
        """Request a parameter that should not exist and classify the error.

        ``ParameterNotFound`` proves the endpoint resolved, the credentials were
        accepted, and ``ssm:GetParameter`` is granted. Errors are classified by
        code, not message. ``kms:Decrypt`` cannot be verified without a real
        SecureString, so a pass does not cover it.
        """
        path = f"{self._prefix}{_PROBE}"
        try:
            self._client.get_parameter(Name=path, WithDecryption=True)
        except ClientError as exc:
            return self._probe_result(exc, path)
        except BotoCoreError as exc:  # no credentials, bad profile, no endpoint
            return self._check(FAIL, f"cannot reach SSM: {exc}")
        # The probe parameter exists; the store still answered.
        return self._check(OK, f"reachable ({self._where()})")

    def _probe_result(self, exc: ClientError, path: str) -> Check:
        code = error_code(exc)
        if code in _NOT_FOUND:
            return self._check(OK, f"reachable ({self._where()})")
        if code in _DENIED:
            return self._check(
                FAIL,
                f"access denied on {path!r} — the caller needs ssm:GetParameter "
                f"(and kms:Decrypt for a SecureString)",
            )
        return self._check(FAIL, f"cannot read SSM: {exc}")

    def _where(self) -> str:
        return f"prefix {self._prefix!r}" if self._prefix else "no prefix"
