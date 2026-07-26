"""The processor Lambda's handler.

Deployed as-is: `code_path` fingerprints this directory at config-evaluation
time, so editing this file changes the digest, and the next plan shows an update.
"""


def handler(event, context):
    return {"received": len(event.get("Records", []))}
