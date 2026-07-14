"""AWS region constants.

Use instead of raw region strings, e.g. ``Stack(region=Region.EuNorth1)`` or
``S3Bucket("b", region=Region.UsWest2)``. Plain ``str`` values, so they pass
straight through validation, hashing, and the provider clients.
"""

from __future__ import annotations


class Region:
    """AWS region codes as static constants (a subset of commonly-used regions)."""

    # North America
    UsEast1 = "us-east-1"
    UsEast2 = "us-east-2"
    UsWest1 = "us-west-1"
    UsWest2 = "us-west-2"
    CaCentral1 = "ca-central-1"

    # Europe
    EuWest1 = "eu-west-1"
    EuWest2 = "eu-west-2"
    EuWest3 = "eu-west-3"
    EuCentral1 = "eu-central-1"
    EuNorth1 = "eu-north-1"
    EuSouth1 = "eu-south-1"

    # Asia Pacific
    ApSouth1 = "ap-south-1"
    ApSoutheast1 = "ap-southeast-1"
    ApSoutheast2 = "ap-southeast-2"
    ApNortheast1 = "ap-northeast-1"
    ApNortheast2 = "ap-northeast-2"

    # South America
    SaEast1 = "sa-east-1"
