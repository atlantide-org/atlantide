"""Atlantide static-website example: S3 + CloudFront + Origin Access Control.

Like ``infra.py``, this is valid Python but run by the deterministic Atlas-lang
interpreter (no clock, randomness, env, or network at config time). ``uuid5`` is an
Atlas-lang *builtin* — a pure derived function the interpreter injects, used without
an import (a static checker flags it as undefined; hence the ``# noqa: F821``).

The site is served from the **default CloudFront domain** (``*.cloudfront.net``), so
it needs no custom domain, ACM certificate, or Route53 records — just four
resources, wired by refs into one graph, in each of two environments:

- **Config** — one declaration of both environments and what differs between them:
  here ``price_class``, so dev serves from the cheapest edge set
  (``PriceClass_100``, North America + Europe) while prod takes the full global
  footprint. Declaring the shape as an :class:`EnvSchema` subclass makes
  ``env.price_class`` an ordinary annotated attribute, so an editor completes it
  and a misspelling is a type error. The value is typed and defaulted, so a
  mistyped prod value fails ``atlantide validate`` rather than the prod apply.
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

Run it (keep it off the other examples' state with a separate db):

    cd examples/aws
    uv run atlantide plan    example-three.py --state site.db
    uv run atlantide apply   example-three.py --state site.db --env dev
    uv run atlantide destroy --state site.db --env dev

Upload an ``index.html`` to the bucket, then open the ``site_url`` output.

NOTE: ``destroy`` disables the distribution and waits for it to redeploy before
deleting it — on real AWS that takes ~15-20 minutes. ``--env`` keeps that wait to
one environment.
"""

from atlantide.core import Config, EnvSchema, Stack, output
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
enforce("deny-destroy-in-protected", stacks=["prod"])  # prod resources are not deletable by apply


class SiteEnv(EnvSchema):
    """What differs between the environments of this site.

    The one class Atlas-lang admits: annotated fields only, no methods and no
    decorators, so it is data. Declaring it lets an editor complete
    `env.price_class` below and flag a misspelling.

    `region`, `tags` and `name_prefix` are well-known keys every environment
    carries, so they need no declaration here.
    """

    #: PriceClass_All | PriceClass_200 | PriceClass_100; defaults to the cheapest.
    price_class: str = "PriceClass_100"


config = Config(
    SiteEnv,
    envs={
        "dev": {
            "region": Region.UsEast1,
            "name_prefix": "atlantide",
            "tags": {"app": "static-site", "env": "dev"},
        },
        "prod": {
            "region": Region.UsEast1,
            "name_prefix": "atlantide",
            "tags": {"app": "static-site", "env": "prod"},
            "price_class": "PriceClass_All",
        },
    },
)

for env in config.envs():
    with Stack(env.name, config=env):
        # Private origin bucket, named explicitly: S3 bucket names are *globally*
        # unique across every AWS account, so the name carries the environment
        # plus a `uuid5` seed (an Atlas-lang builtin, baked into the IR as a
        # fixed value). Without the environment, dev and prod would collide.
        origin = S3Bucket(
            "origin",
            bucket=f"atlantide-site-{env.name}-{uuid5('atlantide-site', env.name)[:8]}",  # noqa: F821
        )

        # OAC lets CloudFront authenticate to the private bucket. Its name is
        # unique per account, so it needs no seed: `oac_name` is omitted and the
        # environment's `name_prefix` composes `{name_prefix}-{oac}-{stack}`
        # (`atlantide-oac-dev`, `atlantide-oac-prod`).
        oac = OriginAccessControl("oac", description=f"OAC for the {env.name} static site")

        # The CDN. `origin_domain`/`oac_id` are refs -> bucket + OAC apply first.
        cdn = CloudFrontDistribution(
            "cdn",
            origin_domain=origin.regional_domain_name,
            oac_id=oac.oac_id,
            default_root_object="index.html",
            price_class=env.price_class,
            comment=f"atlantide static site ({env.name})",
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

        output("site_url", cdn.domain_name)  # https://<id>.cloudfront.net
        output("bucket", origin.bucket)  # the origin bucket name
        output("distribution_id", cdn.distribution_id)  # the CloudFront distribution id
