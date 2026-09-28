"""Read-only lookups of facts about the target account.

They let one config apply to several accounts without hardcoding an account id or
availability zone. Each is read once at apply and pinned in state, so a plan makes
no provider call and two runs of one config lower to identical IR.
"""

from __future__ import annotations

from atlantide.core import DataSource, computed, immutable


class AwsDataSource(DataSource):
    """Base for AWS lookups: the region and account to ask."""

    class Meta:
        provider = "aws"

    region: str = immutable()  # required; a Stack fills it
    #: Credential/endpoint profile, as on ``AwsResource``. Declared here because a
    #: DataSource does not inherit from it.
    provider_alias: str | None = immutable(default=None)


class AwsCallerIdentity(AwsDataSource):
    """The identity behind the current credentials (``sts:GetCallerIdentity``).

    Supplies the account id for building ARNs.
    """

    account_id: str = computed()
    arn: str = computed()
    user_id: str = computed()


class AwsAvailabilityZones(AwsDataSource):
    """The usable availability zones in this region.

    Zone letters differ per account as well as per region, so a hardcoded name such
    as ``eu-north-1a`` is not portable across accounts.
    """

    #: Restrict to zones in this state.
    state: str = immutable(default="available")
    names: list[str] = computed()
    zone_ids: list[str] = computed()
