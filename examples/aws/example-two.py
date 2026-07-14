"""Atlantide showcase: an L2 component, a per-block region override, and output
combinators — three of the newer authoring features in one small graph.

Valid Python syntax, but run by the deterministic Atlas-lang interpreter (no
clock, randomness, env, or network at config time). ``uuid5`` is an Atlas-lang
builtin (a pure derived function the interpreter injects); ``concat`` /
``interpolate`` / ``join`` / ``region`` are imported from ``atlantide.core`` so the
file stays clean Python.

What it demonstrates:

- **Components** — ``SecureBucket`` is a library-authored L2 (an S3 bucket plus a
  TLS-only Deny bucket policy — no public grant, so it applies under S3 Block
  Public Access). Instantiating it expands to flat, namespaced nodes
  (``example:aws.S3Bucket:web-assets`` + ``...:web-policy``); a second instance
  never collides. The component exposes ``.bucket`` / ``.arn`` / ``.domain_name``.
- **Per-block region** — the stack's region is ``eu-north-1``, but the ``logs``
  bucket sits inside ``with region(Region.UsEast1):`` so it is created in
  ``us-east-1`` (e.g. co-located with a us-east-1 CloudFront/ACM setup). The
  override restores on exit — resources after it are back in ``eu-north-1``.
- **Output combinators** — ``concat`` / ``interpolate`` / ``join`` build values
  *from* apply-time refs (bucket ARNs / domains that aren't known until apply).
  They serialize as data and evaluate once the refs resolve, so the graph and its
  content hash stay deterministic.

Run it (keep it off the other examples' state with a separate db):

    uv run atlantide plan    examples/aws/example-two.py --state examples/aws/example-two.db
    uv run atlantide apply   examples/aws/example-two.py --state examples/aws/example-two.db
    uv run atlantide destroy --state examples/aws/example-two.db
"""

from atlantide.core import Stack, concat, interpolate, join, output, region
from atlantide.providers.aws import Region, S3Bucket, SecureBucket

with Stack("example", region=Region.EuNorth1, tags={"app": "showcase"}):
    # --- Component: one call expands into a bucket + a TLS-only Deny policy. ---
    site = SecureBucket("web", bucket=f"atlantide-showcase-{uuid5('showcase', 'web')[:8]}")  # noqa: F821

    # --- Per-block region override: this bucket lives in us-east-1, not the
    #     stack's eu-north-1 (its `region` field is set by the scope). ---
    with region(Region.UsEast1):
        logs = S3Bucket("logs", bucket=f"atlantide-showcase-logs-{uuid5('showcase', 'logs')[:8]}")  # noqa: F821

    # --- Output combinators over apply-time refs (arns/domains unknown until
    #     apply): concat two parts, interpolate a template, join a list. ---
    output("site_url", interpolate("https://{}", site.domain_name))
    output("objects_glob", concat(site.arn, "/*"))            # arn:aws:s3:::web-bucket/*
    output("bucket_domains", join(", ", [site.domain_name, logs.regional_domain_name]))
