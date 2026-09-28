"""Canonical JSON encoder: determinism and key-order invariance."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core import IRError
from atlantide.ir import to_canonical_json
from tests.support.strategies import json_values


def _base_env() -> dict[str, str]:
    """Inherit PATH/venv so the subprocess can import atlantide."""
    keep = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "HOME")
    return {k: os.environ[k] for k in keep if k in os.environ}


def test_key_order_invariance() -> None:
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert to_canonical_json(a) == to_canonical_json(b)
    assert to_canonical_json(a) == b'{"a":2,"b":1,"c":{"y":2,"z":1}}'


def test_primitives() -> None:
    assert to_canonical_json(True) == b"true"
    assert to_canonical_json(False) == b"false"
    assert to_canonical_json(None) == b"null"
    assert to_canonical_json(42) == b"42"
    assert to_canonical_json("hi") == b'"hi"'
    assert to_canonical_json(["a", 1, None]) == b'["a",1,null]'


def test_bool_not_confused_with_int() -> None:
    assert to_canonical_json({"x": True}) == b'{"x":true}'
    assert to_canonical_json({"x": 1}) == b'{"x":1}'


def test_rejects_non_finite() -> None:
    with pytest.raises(IRError):
        to_canonical_json(math.nan)
    with pytest.raises(IRError):
        to_canonical_json(math.inf)


def test_rejects_non_string_keys() -> None:
    with pytest.raises(IRError):
        to_canonical_json({1: "a"})


def test_rejects_unknown_type() -> None:
    with pytest.raises(IRError):
        to_canonical_json(object())


@given(json_values())
def test_encoding_is_stable(value: object) -> None:
    assert to_canonical_json(value) == to_canonical_json(value)


@given(st.dictionaries(st.text(), st.integers(), min_size=1, max_size=8))
def test_shuffled_dict_same_encoding(d: dict[str, int]) -> None:
    reversed_d = dict(reversed(list(d.items())))
    assert to_canonical_json(d) == to_canonical_json(reversed_d)


@given(json_values())
def test_it_agrees_with_the_standard_library(value: object) -> None:
    """Differential property against the standard library as an independent oracle.

    The other properties here compare the encoder to itself, which catches
    instability but not a systematically wrong encoding. `json.dumps` with sorted
    keys and compact separators produces the same bytes for the subset both
    accept, so a disagreement points at the encoder.

    Floats are excluded from the strategy: JCS and `json.dumps` legitimately
    differ on their shortest representation, which belongs in its own test.
    """
    expected = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert to_canonical_json(value).decode("utf-8") == expected


@given(json_values())
def test_the_encoding_is_valid_json_that_reads_back(value: object) -> None:
    """Canonical bytes must still be *JSON*: an encoder emitting almost-JSON would
    hash consistently but be unreadable by everything else."""
    assert json.loads(to_canonical_json(value)) == value


def test_ir_hash_is_stable_across_hash_seeds() -> None:
    """Two runs of the same config produce a byte-identical IR and a stable hash.

    Set ordering is asserted directly in `tests/core/test_silent_coercions.py`.
    This runs the whole lowering pipeline in a fresh interpreter per seed, so a
    container lowered in iteration order is caught here even if no unit test
    covers it.
    """
    prog = (
        "from atlantide.core import ProviderRegistry, Stack, output\n"
        "from atlantide.core.resource import collecting\n"
        "from atlantide.ir import hash_ir, lower\n"
        "from tests.support import Box, Grouped\n"
        "from tests.support.providers import FakeProvider\n"
        "reg = ProviderRegistry()\n"
        "reg.register(FakeProvider())\n"
        "with collecting() as r:\n"
        "    with Stack('s', region='eu'):\n"
        "        a = Box('a', size=1, label='x')\n"
        "        Grouped('g', members={'z', 'y', 'x', 'w', 'v', 'u'})\n"
        "    output('arn', a.out)\n"
        "print(hash_ir(lower(r, reg)))\n"
    )
    hashes = set()
    for seed in ("0", "1", "42", "1337"):
        proc = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],
            env={**_base_env(), "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        hashes.add(proc.stdout.strip())
    assert len(hashes) == 1, f"IR hash depends on PYTHONHASHSEED: {hashes}"
