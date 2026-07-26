"""Read-only lookups: facts about the account this config is being applied to.

Without these, an account id or an availability zone has to be hardcoded, which
means the same config cannot be applied to two accounts — the thing configs are
for.

Read once at apply and pinned in state, so a plan costs no provider call and two
runs of one config still lower to identical IR.
"""

from __future__ import annotations

from atlantide.core import DataSource, computed, immutable


class AwsDataSource(DataSource):
    """Base for AWS lookups: the region and account to ask."""

    class Meta:
        provider = "aws"

    region: str = immutable()  # required (from the stack region)
    #: Credential/endpoint profile, as on a managed resource. Declared here rather
    #: than inherited: a DataSource is not an AwsResource, so it shares the shape
    #: of one without sharing its base.
    provider_alias: str | None = immutable(default=None)


class AwsCallerIdentity(AwsDataSource):
    """Who these credentials are — ``sts:GetCallerIdentity``.

    The account id is the missing piece in every hand-built ARN. Without it a
    config either hardcodes an account (and stops being portable) or cannot name
    its own resources by ARN at all.
    """

    account_id: str = computed()
    arn: str = computed()
    user_id: str = computed()


class AwsAvailabilityZones(AwsDataSource):
    """The usable availability zones in this region.

    Which letters exist differs per account as well as per region, so a config
    that hardcodes ``eu-north-1a`` is a config that works in one account.
    """

    #: Restrict to zones in this state; the default is the useful one.
    state: str = immutable(default="available")
    names: list[str] = computed()
    zone_ids: list[str] = computed()
