"""Atlantide static-website example: S3 + CloudFront + Origin Access Control.

Like ``infra.py``, this is valid Python but run by the deterministic Atlas-lang
interpreter (no clock, randomness, env, or network at config time). ``uuid5`` is an
Atlas-lang *builtin* — a pure derived function the interpreter injects, used without
an import (a static checker flags it as undefined; hence the ``# noqa: F821``).

The site is served from the **default CloudFront domain** (``*.cloudfront.net``), so
it needs no custom domain, ACM certificate, or Route53 records — just four
resources, wired by refs into one graph:

- **S3Bucket** — a private origin bucket (no public access; CloudFront reads it
  through the OAC). Its ``regional_domain_name`` is the CloudFront origin.
- **OriginAccessControl** — lets CloudFront sign requests to the private bucket
  (SigV4). The modern replacement for the legacy Origin Access Identity.
- **CloudFrontDistribution** — the CDN. ``origin_domain`` and ``oac_id`` are refs,
  so the engine creates the bucket and OAC *before* the distribution. ``domain_name``
  (a computed output) is the site URL.
- **S3BucketPolicy** — grants *only this distribution* ``s3:GetObject``, scoped by a
  ``Condition`` on ``AWS:SourceArn`` = the distribution ARN (a ref, so the policy is
  created after the distribution). This is the OAC bucket-policy pattern.

Run it (keep it off ``infra.py``'s state with a separate db):

    uv run atlantide plan    examples/aws/static_site.py --state examples/aws/site.db
    uv run atlantide apply   examples/aws/static_site.py --state examples/aws/site.db
    uv run atlantide destroy --state examples/aws/site.db

Upload an ``index.html`` to the bucket, then open the ``site_url`` output.

NOTE: ``destroy`` disables the distribution and waits for it to redeploy before
deleting it — on real AWS that takes ~15-20 minutes.
"""

from atlantide.core import Stack, output
from atlantide.policy import enforce
from atlantide.providers.aws import (
    CloudFrontDistribution,
    OriginAccessControl,
    Region,
    S3Bucket,
    S3BucketPolicy,
    ServicePrincipal,
    allow,
)

enforce("require-tags")  # plan-time policy: every taggable resource must carry tags

with Stack("example", region=Region.UsEast1, name_prefix="atlantide", tags={"app": "static-site"}):
    # Private origin bucket. `uuid5` (an Atlas-lang builtin) gives it a stable,
    # globally-unique name baked into the IR.
    origin = S3Bucket("origin", bucket=f"atlantide-site-{uuid5('atlantide-site', 'origin')[:8]}")  # noqa: F821

    # OAC lets CloudFront authenticate to the private bucket.
    oac = OriginAccessControl("oac", oac_name="site-oac", description="OAC for the static site")

    # The CDN. `origin_domain`/`oac_id` are refs -> bucket + OAC apply first.
    cdn = CloudFrontDistribution(
        "cdn",
        origin_domain=origin.regional_domain_name,
        oac_id=oac.oac_id,
        default_root_object="index.html",
        comment="atlantide static site",
    )

    # Bucket policy: only this distribution (via OAC) may read the objects. The
    # `AWS:SourceArn` condition references the distribution ARN, so the policy is
    # ordered after the distribution.
    S3BucketPolicy(
        "origin-policy",
        bucket=origin.bucket,  # ref -> bucket applies before its policy
        statements=[
            allow(
                S3Bucket.Action.GetObject,
                on=origin.objects_arn,
                principal={"Service": ServicePrincipal.CloudFront},
                condition={"StringEquals": {"AWS:SourceArn": cdn.arn}},
            )
        ],
    )

    output("site_url", cdn.domain_name)          # https://<id>.cloudfront.net
    output("bucket", origin.bucket)              # the origin bucket name
    output("distribution_id", cdn.distribution_id)  # the CloudFront distribution id
