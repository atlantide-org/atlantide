# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the ``atlantide`` CLI (onefile).

Build:  pyinstaller atlantide.spec --clean --noconfirm  ->  dist/atlantide

Two things PyInstaller cannot infer on its own for this project:

1. Providers/components are imported dynamically by name at runtime
   (``importlib.import_module`` in ``atlantide.lang.interp``), so static analysis
   never sees them. ``collect_submodules("atlantide")`` force-includes every
   ``atlantide.*`` submodule.
2. ``botocore``/``boto3`` ship large JSON data dirs. PyInstaller's contrib hooks
   (``hook-botocore``/``hook-boto3``) already collect these in full, plus the
   ``cryptography``/``pydantic-core`` binaries — so we do NOT collect them here.
   Instead we PRUNE the collected set below (see ``_keep_data``): the hooks pull
   ~22MB of service models for every AWS service, but clients are created lazily
   per service (``AwsProvider._client``), so only the handful atlantide's handlers
   use are ever loaded. Pruning the rest is the main size win (~17MB).
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("atlantide")

a = Analysis(
    ["scripts/atlantide_entry.py"],
    pathex=[],
    binaries=[],
    datas=[],  # botocore/boto3 data is added by their contrib hooks, then pruned below
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "hypothesis", "mypy", "ruff", "moto"],  # dev-only; trim size
    noarchive=False,
)

# --- prune the data the contrib hooks over-collected --------------------------
# botocore loads a service's models only when a client for it is created. Keep the
# services atlantide actually uses (handler.service values), STS + SSO for
# credential/identity resolution, and the shared top-level data (endpoints,
# partitions, retry config, TLS CA bundle). Drop every other service dir, plus
# doc-only example files (botocore ``examples-1.json`` and boto3 ``*.rst``).
_KEEP_SERVICES = {
    "acm", "cloudfront", "dynamodb", "ec2", "iam", "lambda", "logs",
    "route53", "s3", "sns", "sqs",            # handler.service values
    "sts", "sso", "sso-oidc",                 # credential / identity resolution
}


def _keep_data(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if d.endswith("examples-1.json") or d.startswith("boto3/examples/"):
        return False  # doc-only, never loaded at runtime
    if d.startswith("boto3/data/"):
        return False  # boto3 resource models; atlantide uses .client() only, never .resource()
    if d.startswith("botocore/data/"):
        rest = d[len("botocore/data/"):]
        if "/" not in rest:
            return True  # shared top-level file (endpoints.json, partitions.json, ...)
        return rest.split("/", 1)[0] in _KEEP_SERVICES
    return True


a.datas = [entry for entry in a.datas if _keep_data(entry[0])]

pyz = PYZ(a.pure)

# Onefile form: pass a.binaries + a.datas straight into EXE and omit COLLECT.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="atlantide",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)
