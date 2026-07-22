"""Remote secrets backend: AWS SSM Parameter Store.

A ``SecretRef("db_password", provider="ssm")`` resolves to the decrypted value of
the parameter at ``{prefix}db_password``. Like every other
:class:`~atlantide.secrets.backend.SecretsProvider`, the value is fetched
in-memory at apply time and never written to config, the IR, or state — state
keeps only the rotation digest.

Values are memoised per instance, so a config referencing one secret from several
resources costs one API call and yields one consistent value for the whole run.
"""

from __future__ import annotations

from typing import Any, ClassVar

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets.backend import SecretsProvider

#: Error codes meaning "the store answered, that name just isn't in it".
_NOT_FOUND = frozenset({"ParameterNotFound"})

#: Error codes meaning "the store answered, and refused you".
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
        self._client: Any = make_client("ssm", endpoint_url=endpoint_url)
        self._cache: dict[str, str] = {}

    def resolve(self, name: str) -> str:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        path = f"{self._prefix}{name}"
        try:
            response = self._client.get_parameter(Name=path, WithDecryption=True)
        except ClientError as exc:
            raise self._error(exc, name, path) from exc
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
        code = _code(exc)
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

    def check(self) -> Check:
        """Ask for a name that cannot exist, and read the refusal.

        A "no such parameter" is the answer that proves the most: the endpoint
        resolved, the credentials were accepted, and ``ssm:GetParameter`` is
        granted — everything that would otherwise fail mid-apply. The error
        *code* distinguishes that from a denial, so this never has to match on
        the wording of a message.

        ``kms:Decrypt`` cannot be verified without a real SecureString to read,
        so a pass here does not promise one.
        """
        path = f"{self._prefix}{_PROBE}"
        try:
            self._client.get_parameter(Name=path, WithDecryption=True)
        except ClientError as exc:
            return self._probe_result(exc, path)
        except BotoCoreError as exc:  # no credentials, bad profile, no endpoint
            return Check(f"secrets: {self.name}", FAIL, f"cannot reach SSM: {exc}")
        # Someone really did create the probe parameter; the store still answered.
        return Check(f"secrets: {self.name}", OK, f"reachable ({self._where()})")

    def _probe_result(self, exc: ClientError, path: str) -> Check:
        code = _code(exc)
        if code in _NOT_FOUND:
            return Check(f"secrets: {self.name}", OK, f"reachable ({self._where()})")
        if code in _DENIED:
            return Check(
                f"secrets: {self.name}",
                FAIL,
                f"access denied on {path!r} — the caller needs ssm:GetParameter "
                f"(and kms:Decrypt for a SecureString)",
            )
        return Check(f"secrets: {self.name}", FAIL, f"cannot read SSM: {exc}")

    def _where(self) -> str:
        return f"prefix {self._prefix!r}" if self._prefix else "no prefix"


def _code(exc: ClientError) -> str:
    """The AWS error code, e.g. ``ParameterNotFound`` (``""`` if the shape is odd)."""
    return str(exc.response.get("Error", {}).get("Code", ""))
