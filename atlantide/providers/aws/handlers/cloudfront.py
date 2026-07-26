"""CloudFront handlers: origin access control and distribution.

CloudFront resources are id-located: read/update/delete resolve the id restored
from state (``known_id``). Every mutating call needs a fresh ``IfMatch`` ETag, so
a ``get`` runs immediately before an update or delete. ``CallerReference`` is the
stable ``node_id``, so a retried create is idempotent (AWS rejects a duplicate
reference).
"""

from __future__ import annotations

import time
from typing import Any

from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    create_or_adopt,
    ignore_missing,
    known_id,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import absent_ok, not_found
from atlantide.providers.aws.handlers.pagination import marker_pages
from atlantide.providers.aws.resources import CloudFrontDistribution, OriginAccessControl
from atlantide.providers.aws.resources.cloudfront import CACHING_OPTIMIZED

_ORIGIN_ID = "s3-origin"

#: Bounded poll for a distribution to reach ``Deployed`` before delete; on real
#: AWS this takes several minutes after a disable.
_DEPLOY_POLL_ATTEMPTS = 120
_DEPLOY_POLL_DELAY = 15.0


class CloudFrontOacHandler(AwsHandler[OriginAccessControl]):
    service = "cloudfront"
    resource_type = OriginAccessControl
    identity_field = "oac_id"

    @override
    def create(self, client: Any, res: OriginAccessControl) -> dict[str, Any]:
        resp = client.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": res.oac_name,
                "Description": res.description,
                "OriginAccessControlOriginType": "s3",
                "SigningBehavior": "always",
                "SigningProtocol": "sigv4",
            }
        )
        return {"oac_id": resp["OriginAccessControl"]["Id"]}

    @override
    def read(self, client: Any, res: OriginAccessControl) -> dict[str, Any] | None:
        oid = known_id(res, self.identity_field) or self._find(client, res.oac_name)
        if oid is None:
            return None
        if absent_ok(lambda: client.get_origin_access_control(Id=oid)) is None:
            return None
        return {"oac_id": oid}

    @override
    def update(
        self, client: Any, prior: dict[str, Any], res: OriginAccessControl
    ) -> dict[str, Any]:
        oid = prior.get(self.identity_field) or known_id(res, self.identity_field)
        if oid is None:  # update runs only on an existing control
            raise not_found(res, "update")
        got = client.get_origin_access_control(Id=oid)
        config = got["OriginAccessControl"]["OriginAccessControlConfig"]
        config["Description"] = res.description
        client.update_origin_access_control(
            Id=oid, IfMatch=got["ETag"], OriginAccessControlConfig=config
        )
        return {"oac_id": oid}

    @override
    def delete(self, client: Any, res: OriginAccessControl) -> None:
        oid = known_id(res, self.identity_field)
        if oid is None:
            return
        with ignore_missing():
            got = client.get_origin_access_control(Id=oid)
            client.delete_origin_access_control(Id=oid, IfMatch=got["ETag"])

    @staticmethod
    def _find(client: Any, name: str) -> str | None:
        # Every page, not just the first: an account past one page of OACs would
        # otherwise read a healthy control as absent — refresh classifies it
        # MISSING and `refresh --write` drops its state row.
        pages = marker_pages(client.list_origin_access_controls, "OriginAccessControlList")
        return next((str(item["Id"]) for item in pages if item.get("Name") == name), None)


class CloudFrontDistributionHandler(AwsHandler[CloudFrontDistribution]):
    service = "cloudfront"
    resource_type = CloudFrontDistribution
    identity_field = "distribution_id"

    @override
    def create(self, client: Any, res: CloudFrontDistribution) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            config = _distribution_config(res)
            if res.tags:
                resp = client.create_distribution_with_tags(
                    DistributionConfigWithTags={
                        "DistributionConfig": config,
                        "Tags": {"Items": tag_list(res.tags)},
                    }
                )
            else:
                resp = client.create_distribution(DistributionConfig=config)
            return _distribution_outputs(resp["Distribution"])

        # The stable CallerReference makes a re-run create answer
        # DistributionAlreadyExists rather than provision a second distribution;
        # adopting the one holding the reference is what makes that idempotent
        # rather than merely an error.
        return create_or_adopt(make, lambda: self._find_by_reference(client, res.node_id))

    @staticmethod
    def _find_by_reference(client: Any, reference: str) -> dict[str, Any] | None:
        """The distribution created under this ``CallerReference``, or ``None``.

        Distribution summaries do not carry the reference, so each candidate
        costs a ``get``; adoption only runs on a create conflict, never on the
        steady-state path. Every page, for the same reason as the OAC ``_find``.
        """
        for item in marker_pages(client.list_distributions, "DistributionList"):
            got = client.get_distribution(Id=item["Id"])["Distribution"]
            if got["DistributionConfig"].get("CallerReference") == reference:
                return _distribution_outputs(got)
        return None

    @override
    def read(self, client: Any, res: CloudFrontDistribution) -> dict[str, Any] | None:
        did = known_id(res, self.identity_field)
        if did is None:
            return None
        got = absent_ok(lambda: client.get_distribution(Id=did))
        if got is None:
            return None
        return _distribution_outputs(got["Distribution"])

    @override
    def update(
        self, client: Any, prior: dict[str, Any], res: CloudFrontDistribution
    ) -> dict[str, Any]:
        did = prior.get(self.identity_field) or known_id(res, self.identity_field)
        if did is None:  # update runs only on an existing distribution
            raise not_found(res, "update")
        got = client.get_distribution(Id=did)
        config = _apply_desired(got["Distribution"]["DistributionConfig"], res)
        updated = client.update_distribution(Id=did, IfMatch=got["ETag"], DistributionConfig=config)
        outputs = _distribution_outputs(updated["Distribution"])
        # CloudFront wraps both directions in an `Items` envelope.
        arn = outputs["arn"]
        sync_tags(
            res.tags,
            live=lambda: tags_from_list(
                client.list_tags_for_resource(Resource=arn).get("Tags", {}).get("Items", [])
            ),
            untag=lambda stale, _: client.untag_resource(Resource=arn, TagKeys={"Items": stale}),
            tag=lambda tags: client.tag_resource(Resource=arn, Tags={"Items": tag_list(tags)}),
        )
        return outputs

    @override
    def delete(self, client: Any, res: CloudFrontDistribution) -> None:
        did = known_id(res, self.identity_field)
        if did is None:
            return
        with ignore_missing():
            got = client.get_distribution(Id=did)
            config = got["Distribution"]["DistributionConfig"]
            etag = got["ETag"]
            if config["Enabled"]:  # a distribution must be disabled before deletion
                config["Enabled"] = False
                etag = client.update_distribution(Id=did, IfMatch=etag, DistributionConfig=config)[
                    "ETag"
                ]
            etag = self._wait_deployed(client, did, etag)
            client.delete_distribution(Id=did, IfMatch=etag)

    @staticmethod
    def _wait_deployed(client: Any, did: str, etag: str) -> str:
        for _ in range(_DEPLOY_POLL_ATTEMPTS):
            got = client.get_distribution(Id=did)
            etag = got["ETag"]
            if got["Distribution"]["Status"] == "Deployed":
                break
            time.sleep(_DEPLOY_POLL_DELAY)
        return etag


def _apply_desired(config: dict[str, Any], res: CloudFrontDistribution) -> dict[str, Any]:
    """Write every mutable field of ``res`` onto ``config``.

    Shared by create (over a fresh skeleton) and update (over the fetched live
    config), so a field added to the resource cannot land in one and not the
    other — which would make it a silent no-op on update.
    """
    config["Comment"] = res.comment
    config["Enabled"] = res.enabled
    config["DefaultRootObject"] = res.default_root_object
    config["Origins"]["Items"][0]["OriginAccessControlId"] = res.oac_id
    config["Aliases"] = {"Quantity": len(res.aliases), "Items": list(res.aliases)}
    config["PriceClass"] = res.price_class
    config["ViewerCertificate"] = _viewer_certificate(res)
    return config


def _distribution_config(res: CloudFrontDistribution) -> dict[str, Any]:
    """The full create payload: the immutable skeleton plus the mutable fields."""
    return _apply_desired(
        {
            "CallerReference": res.node_id,  # stable reference; a retried create is idempotent
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": _ORIGIN_ID,
                        "DomainName": res.origin_domain,
                        "OriginAccessControlId": res.oac_id,
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": _ORIGIN_ID,
                "ViewerProtocolPolicy": "redirect-to-https",
                "CachePolicyId": CACHING_OPTIMIZED,
            },
        },
        res,
    )


def _viewer_certificate(res: CloudFrontDistribution) -> dict[str, Any]:
    """Which certificate serves the aliases, or CloudFront's own.

    Without a certificate a distribution is reachable only at its
    ``*.cloudfront.net`` name — which is why an `AcmCertificate` existed with
    nothing able to consume it. ``SNI-only`` is the modern default; dedicated IPs
    cost money and buy compatibility with clients that no longer exist.
    """
    if res.certificate_arn is None:
        return {"CloudFrontDefaultCertificate": True}
    return {
        "ACMCertificateArn": res.certificate_arn,
        "SSLSupportMethod": "sni-only",
        "MinimumProtocolVersion": res.minimum_protocol_version,
    }


def _distribution_outputs(distribution: dict[str, Any]) -> dict[str, Any]:
    return {
        "distribution_id": distribution["Id"],
        "domain_name": distribution["DomainName"],
        "arn": distribution["ARN"],
    }
