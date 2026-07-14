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

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, known_id, tag_list
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

    def read(self, client: Any, res: OriginAccessControl) -> dict[str, Any] | None:
        oid = known_id(res, "oac_id") or self._find(client, res.oac_name)
        if oid is None:
            return None
        try:
            client.get_origin_access_control(Id=oid)
        except client.exceptions.ClientError:
            return None
        return {"oac_id": oid}

    def update(
        self, client: Any, prior: dict[str, Any], res: OriginAccessControl
    ) -> dict[str, Any]:
        oid = prior.get("oac_id") or known_id(res, "oac_id")
        got = client.get_origin_access_control(Id=oid)
        config = got["OriginAccessControl"]["OriginAccessControlConfig"]
        config["Description"] = res.description
        client.update_origin_access_control(
            Id=oid, IfMatch=got["ETag"], OriginAccessControlConfig=config
        )
        return {"oac_id": oid}

    def delete(self, client: Any, res: OriginAccessControl) -> None:
        oid = known_id(res, "oac_id")
        if oid is None:
            return
        with ignore_missing():
            got = client.get_origin_access_control(Id=oid)
            client.delete_origin_access_control(Id=oid, IfMatch=got["ETag"])

    @staticmethod
    def _find(client: Any, name: str) -> str | None:
        resp = client.list_origin_access_controls()
        items = resp.get("OriginAccessControlList", {}).get("Items", [])
        return next((i["Id"] for i in items if i.get("Name") == name), None)


class CloudFrontDistributionHandler(AwsHandler[CloudFrontDistribution]):
    service = "cloudfront"
    resource_type = CloudFrontDistribution

    def create(self, client: Any, res: CloudFrontDistribution) -> dict[str, Any]:
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

    def read(self, client: Any, res: CloudFrontDistribution) -> dict[str, Any] | None:
        did = known_id(res, "distribution_id")
        if did is None:
            return None
        try:
            got = client.get_distribution(Id=did)
        except client.exceptions.ClientError:
            return None
        return _distribution_outputs(got["Distribution"])

    def update(
        self, client: Any, prior: dict[str, Any], res: CloudFrontDistribution
    ) -> dict[str, Any]:
        did = prior.get("distribution_id") or known_id(res, "distribution_id")
        got = client.get_distribution(Id=did)
        config = got["Distribution"]["DistributionConfig"]
        config["Comment"] = res.comment
        config["Enabled"] = res.enabled
        config["DefaultRootObject"] = res.default_root_object
        config["Origins"]["Items"][0]["OriginAccessControlId"] = res.oac_id
        updated = client.update_distribution(
            Id=did, IfMatch=got["ETag"], DistributionConfig=config
        )
        outputs = _distribution_outputs(updated["Distribution"])
        if res.tags:
            client.tag_resource(
                Resource=outputs["arn"], Tags={"Items": tag_list(res.tags)}
            )
        return outputs

    def delete(self, client: Any, res: CloudFrontDistribution) -> None:
        did = known_id(res, "distribution_id")
        if did is None:
            return
        with ignore_missing():
            got = client.get_distribution(Id=did)
            config = got["Distribution"]["DistributionConfig"]
            etag = got["ETag"]
            if config["Enabled"]:  # a distribution must be disabled before deletion
                config["Enabled"] = False
                etag = client.update_distribution(
                    Id=did, IfMatch=etag, DistributionConfig=config
                )["ETag"]
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


def _distribution_config(res: CloudFrontDistribution) -> dict[str, Any]:
    return {
        "CallerReference": res.node_id,  # stable reference; a retried create is idempotent
        "Comment": res.comment,
        "Enabled": res.enabled,
        "DefaultRootObject": res.default_root_object,
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
    }


def _distribution_outputs(distribution: dict[str, Any]) -> dict[str, Any]:
    return {
        "distribution_id": distribution["Id"],
        "domain_name": distribution["DomainName"],
        "arn": distribution["ARN"],
    }
