"""The shared one-line helpers: engine builders, config writer, changeset and log readers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from atlantide.providers import local
from atlantide.providers import random as random_provider
from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend
from tests.support import (
    TEST_REGION,
    actions_of,
    aws_engine,
    aws_fixture,
    debug_records,
    leaves,
    local_engine,
    random_engine,
    write_config,
)

#: Moto, fake credentials and a default ``Stack`` in ``TEST_REGION`` for every test
#: here, as in each AWS suite; the local and random tests do not notice it.
aws_env = aws_fixture()


def _file_config(tmp: Path) -> str:
    # b's content reads a's computed checksum: a real edge between the two.
    return (
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp / 'a.txt')!r}, content='alpha')\n"
        f"File('b', path={str(tmp / 'b.txt')!r}, content=a.checksum)\n"
    )


# -- write_config -----------------------------------------------------------------


def test_write_config_writes_config_py_and_returns_its_path(tmp_path: Path) -> None:
    path = write_config(tmp_path, "x = 1\n")

    assert path == tmp_path / "config.py"
    assert path.read_text(encoding="utf-8") == "x = 1\n"


def test_write_config_takes_a_file_name_and_overwrites(tmp_path: Path) -> None:
    write_config(tmp_path, "old\n", name="infra.py")
    path = write_config(tmp_path, "new — é\n", name="infra.py")

    assert path == tmp_path / "infra.py"
    assert path.read_text(encoding="utf-8") == "new — é\n"


# -- engine builders ----------------------------------------------------------------


async def test_local_engine_plans_and_applies_local_files(tmp_path: Path) -> None:
    engine = local_engine()
    assert engine.types == local.TYPES
    assert isinstance(engine.backend, MemoryStateBackend)

    planned = engine.plan(_file_config(tmp_path)).unwrap()
    assert set(actions_of(planned.changeset).values()) == {Action.CREATE}

    report = (await engine.apply(_file_config(tmp_path))).unwrap()
    assert sorted(report.created) == ["default:local.File:a", "default:local.File:b"]
    assert (tmp_path / "a.txt").read_text() == "alpha"


def test_local_engine_forwards_keywords_to_make_engine() -> None:
    backend = MemoryStateBackend()

    engine = local_engine(backend=backend, parallelism=3)

    assert engine.backend is backend


async def test_random_engine_generates_and_pins_values() -> None:
    engine = random_engine()
    assert engine.types == random_provider.TYPES
    src = "from atlantide.providers.random import Uuid\nUuid('u')\n"

    first = (await engine.apply(src)).unwrap()
    value = engine.backend.load().get("default:random.Uuid:u").outputs["result"]
    second = (await engine.apply(src)).unwrap()

    assert first.created == ["default:random.Uuid:u"]
    assert second.noop == ["default:random.Uuid:u"]
    assert engine.backend.load().get("default:random.Uuid:u").outputs["result"] == value


def test_random_engine_forwards_keywords_to_make_engine() -> None:
    backend = MemoryStateBackend()

    assert random_engine(backend=backend).backend is backend


class TestAwsEngine:
    """Under moto (the module-level ``aws_env``), like every suite that builds one."""

    def test_registers_the_aws_provider_in_the_test_region(self) -> None:
        from atlantide.providers.aws import TYPES, AwsProvider

        engine = aws_engine()

        provider = engine.providers.get("aws").unwrap()
        assert isinstance(provider, AwsProvider)
        assert provider.region == TEST_REGION
        assert engine.types == TYPES
        assert isinstance(engine.backend, MemoryStateBackend)

    def test_uses_the_backend_it_is_given(self) -> None:
        backend = MemoryStateBackend()

        assert aws_engine(backend).backend is backend

    async def test_applies_against_the_mock(self) -> None:
        engine = aws_engine()
        src = (
            "from atlantide.providers.aws import S3Bucket\n"
            "S3Bucket('b', bucket='wp0-helper-bucket')\n"
        )

        report = (await engine.apply(src, "infra.py")).unwrap()

        assert report.created == ["default:aws.S3Bucket:b"]


# -- actions_of -------------------------------------------------------------------


async def test_actions_of_maps_every_node_including_noops(tmp_path: Path) -> None:
    engine = local_engine()
    src = _file_config(tmp_path)
    (await engine.apply(src)).unwrap()

    changeset = engine.plan(src).unwrap().changeset

    assert actions_of(changeset) == {
        "default:local.File:a": Action.NOOP,
        "default:local.File:b": Action.NOOP,
    }


# -- debug_records ----------------------------------------------------------------


def test_debug_records_captures_debug_even_when_the_logger_is_quieter() -> None:
    logger = logging.getLogger("tests.support.helpers.probe")
    logger.setLevel(logging.WARNING)

    with debug_records("tests.support.helpers.probe") as records:
        logger.debug("value=%d", 7)
        logging.getLogger("tests.support.helpers.other").warning("not mine")

    assert [r.getMessage() for r in records] == ["value=7"]
    assert records[0].levelno == logging.DEBUG


def test_debug_records_restores_level_and_handlers_even_on_error() -> None:
    logger = logging.getLogger("tests.support.helpers.restore")
    logger.setLevel(logging.ERROR)
    handlers = list(logger.handlers)

    with pytest.raises(RuntimeError), debug_records("tests.support.helpers.restore") as records:
        raise RuntimeError("boom")
    logger.debug("after the block")

    assert logger.level == logging.ERROR
    assert logger.handlers == handlers
    assert records == []


# -- leaves -----------------------------------------------------------------------


def test_leaves_of_a_plain_exception_is_itself() -> None:
    err = ValueError("x")

    assert leaves(err) == [err]


def test_leaves_flattens_nested_groups_in_order() -> None:
    a, b, c, d = ValueError("a"), KeyError("b"), RuntimeError("c"), KeyboardInterrupt()
    group = BaseExceptionGroup("outer", [a, ExceptionGroup("inner", [b, c]), d])

    assert leaves(group) == [a, b, c, d]
