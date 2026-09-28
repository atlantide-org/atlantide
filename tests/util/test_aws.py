"""`atlantide.util.aws`: the error-shape readers and the shared client config.

A config from the helper must be indistinguishable from the botocore `Config`
each consumer declares.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from atlantide import __version__
from atlantide.core.tuning import MIN_POOL
from atlantide.providers.aws.config import boto_config
from atlantide.providers.aws.handlers import faults
from atlantide.util.aws import client_config, error_code, error_message


def _client_error(error: dict[str, Any] | None) -> ClientError:
    response: dict[str, Any] = {} if error is None else {"Error": error}
    return ClientError(response, "SomeOperation")  # type: ignore[arg-type]


# -- error_code / error_message ------------------------------------------------


def test_error_code_reads_the_code() -> None:
    assert error_code(_client_error({"Code": "NoSuchKey", "Message": "gone"})) == "NoSuchKey"


@pytest.mark.parametrize("error", [None, {}, {"Code": None}])
def test_error_code_is_empty_when_the_response_has_none(error: dict[str, Any] | None) -> None:
    assert error_code(_client_error(error)) == ""


def test_error_code_stringifies_a_non_string_code() -> None:
    assert error_code(_client_error({"Code": 404})) == "404"


@pytest.mark.parametrize(
    "error",
    [None, {}, {"Code": None}, {"Code": "ThrottlingException"}, {"Code": 404}],
)
def test_error_code_matches_the_faults_semantics(error: dict[str, Any] | None) -> None:
    """`error_code` shares `faults.error_code`'s semantics."""
    exc = _client_error(error)
    assert error_code(exc) == faults.error_code(exc)


def test_error_message_reads_the_message() -> None:
    assert error_message(_client_error({"Code": "X", "Message": "no change"})) == "no change"


@pytest.mark.parametrize("error", [None, {}, {"Message": None}])
def test_error_message_is_empty_when_the_response_has_none(
    error: dict[str, Any] | None,
) -> None:
    assert error_message(_client_error(error)) == ""


# -- client_config -------------------------------------------------------------


def _same_config(left: Config, right: Config) -> None:
    """Equal in every option botocore knows and in which options were passed.

    `_user_provided_options` is what botocore merges on `Config.merge`, so a
    helper that passed an extra option explicitly (even at its default value)
    would change merge behaviour without changing any attribute.
    """
    assert left._user_provided_options == right._user_provided_options  # type: ignore[attr-defined]
    for option in Config.OPTION_DEFAULTS:
        assert getattr(left, option) == getattr(right, option), option


def _module_configs_match() -> None:
    from atlantide.secrets.ssm import _CLIENT_CONFIG as ssm_config
    from atlantide.state.s3.dynamo import CLIENT_CONFIG as s3_config

    _same_config(client_config(max_pool=64), s3_config)
    _same_config(client_config(), ssm_config)


def test_client_config_reproduces_the_module_level_configs() -> None:
    """The S3 state backend's config (pool 64) and SSM's (no pool, no user agent).

    Checked in a fresh interpreter: creating a client rewrites the `retries` of
    the `Config` it was given in place (`max_attempts` becomes
    `total_max_attempts`), so once any other test has built a client from these
    shared module-level configs they no longer look as they were constructed.
    """
    code = "from tests.util.test_aws import _module_configs_match; _module_configs_match()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("parallelism", [1, MIN_POOL, 64])
def test_client_config_reproduces_the_provider_config(parallelism: int) -> None:
    _same_config(
        client_config(max_pool=max(MIN_POOL, parallelism), user_agent=f"atlantide/{__version__}"),
        boto_config(parallelism=parallelism),
    )


def test_client_config_leaves_unset_options_to_botocore() -> None:
    config = client_config()
    provided = config._user_provided_options  # type: ignore[attr-defined]
    assert "max_pool_connections" not in provided
    assert "user_agent_extra" not in provided


def test_importing_the_module_does_not_import_botocore() -> None:
    """`providers.aws.config` is imported by every CLI command; keep botocore lazy."""
    code = "import sys, atlantide.util.aws; sys.exit('botocore' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
