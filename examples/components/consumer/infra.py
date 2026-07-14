"""Consume a published component.

After `atlantide component add ... --as site`, the vendored component is importable
under `atlantide.components.<alias>` — an ordinary Atlas-lang import that passes the
config sandbox (only `atlantide.*` is importable). Everything else here is normal
config: a Stack for the region, then instantiate the component like any other.
"""

from atlantide.components.site import SecureSite

from atlantide.core import Stack, output

with Stack("prod", region="eu-north-1"):
    site = SecureSite("cdn", bucket="example-prod-assets")

output("bucket_arn", site.arn)
