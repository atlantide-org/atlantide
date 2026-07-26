"""The two AWS helpers whose wrong answer is silent.

``is_missing`` decides whether ``refresh --write`` deletes a state row, and
``stale_tag_keys`` decides whether a tag the config dropped is ever removed.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from atlantide.providers.aws.handlers.base import is_missing, stale_tag_keys


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Op")


@pytest.mark.parametrize(
    "code, expected",
    [
        ("NoSuchBucket", True),
        ("404", True),
        ("AccessDenied", False),
        ("ThrottlingException", False),
        ("InternalError", False),
    ],
)
def test_only_real_absence_reads_as_missing(code: str, expected: bool) -> None:
    """`head_bucket` answers 403 for a bucket that exists but is not readable;
    treating that as absence makes `refresh --write` delete the state row."""
    assert is_missing(_client_error(code)) is expected


def test_stale_tag_keys_reports_what_config_dropped() -> None:
    """AWS tagging APIs are additive, so writing the remaining tags never removes."""
    assert stale_tag_keys({"a": "1", "b": "2"}, {"a": "1"}) == ["b"]
    assert stale_tag_keys({"a": "1"}, {}) == ["a"]
    assert stale_tag_keys({}, {"a": "1"}) == []
