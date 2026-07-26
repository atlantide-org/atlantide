"""No secret reaches a log line or a state file, for any shape of payload.

This is the one failure in the project that cannot be rolled back. A wrong plan
can be re-planned and a failed apply can be retried, but a secret written to a log
has been disclosed to everything downstream of that log — the shipper, the index,
the retention window, the person who has read access to none of the rest.

The module docstring for `core.logging` claims redaction happens "by
construction… so a secret handle or a sealed value cannot reach a log file by
someone forgetting to think about it at a call site". That is a claim about
*every* payload, which is what makes it a property rather than an example. The
shapes that break it are the ones nobody writes a test for: not the dict with a
secret in it, but the set, the `Transform`, and the nested pydantic model — the
containers a hand-rolled walker stops at. Those three were a real gap here, and
`redact` now delegates to the same walker the hashing path uses so that the
answer to "what counts as a child" cannot differ between them.

The sealing properties sit alongside because they are the same guarantee at rest
rather than in transit, and one of them is easy to get subtly wrong: a codec that
fails *open* — returning a wrong plaintext from tampered ciphertext rather than
refusing — hands the caller a value it will happily use. AES-GCM's tag exists to
make that impossible, so the property asserts refusal on arbitrary corruption.
"""

from __future__ import annotations

import json
import logging
from base64 import b64decode, b64encode
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from atlantide.core.errors import SecretsError
from atlantide.core.logging import REDACTED, JsonFormatter, RedactingFilter, redact
from atlantide.core.types import SEALED_KEY, SECRET_MARKER_KEYS
from atlantide.secrets.digest import secret_digest
from atlantide.secrets.material import SEAL_V2_PREFIX, KeyMaterial, is_sealed_marker
from tests.support.strategies import (
    SECRET_SENTINEL,
    property_trees,
    secret_marker_trees,
)


@pytest.fixture(scope="session")
def material(tmp_path_factory: pytest.TempPathFactory) -> KeyMaterial:
    """One key for the whole module.

    Session-scoped deliberately: a function-scoped fixture under `@given` trips
    `HealthCheck.function_scoped_fixture`, which this repo's Hypothesis profiles
    do not suppress — and re-deriving a key per example would buy nothing but
    seconds spent in `os.urandom`.
    """
    keyfile: Path = tmp_path_factory.mktemp("secrets") / "key"
    return KeyMaterial(str(keyfile))


def _serialize(value: Any) -> str:
    """Everything in ``value``, flattened to text the way a log sink sees it."""
    return json.dumps(value, default=repr)


# -- redaction ---------------------------------------------------------------


@given(secret_marker_trees())
def test_no_secret_survives_redaction_anywhere_in_a_tree(tree: Any) -> None:
    """The headline property: depth and container kind do not matter.

    Scanning the serialized output rather than inspecting structure is the point.
    A structural check would have to know where to look, and "somewhere nobody
    thought to look" is the whole failure mode.
    """
    redacted = _serialize(redact(tree))

    assert SECRET_SENTINEL not in redacted
    for marker in SECRET_MARKER_KEYS:
        assert marker not in redacted


@given(secret_marker_trees())
def test_no_secret_reaches_a_formatted_log_line(tree: Any) -> None:
    """End-to-end through the surface that actually writes to disk.

    `redact` being correct is necessary but not sufficient: the filter decides
    *which* record attributes to pass through it, and the formatter decides which
    to emit. A field the filter's standard-attribute exclusion skipped would be
    redacted nowhere and printed anyway.
    """
    record = logging.LogRecord("atlantide.test", logging.INFO, __file__, 0, "applied", None, None)
    record.__dict__["outputs"] = tree

    assert RedactingFilter().filter(record)
    line = JsonFormatter().format(record)

    assert SECRET_SENTINEL not in line
    for marker in SECRET_MARKER_KEYS:
        assert marker not in line


@given(property_trees())
def test_redaction_never_raises_on_any_property_tree(tree: Any) -> None:
    """Totality, over the same generator the hashing path is tested with.

    `redact` runs inside a logging filter. A filter that raises does not degrade
    to an unredacted line — it takes down the log call, and with it whatever the
    caller was in the middle of reporting. A `namedtuple` used to do exactly that.
    """
    redact(tree)


@given(property_trees())
def test_redaction_is_idempotent(tree: Any) -> None:
    assert redact(redact(tree)) == redact(tree)


def test_redaction_survives_a_named_tuple() -> None:
    """A regression, kept as an example because the generators do not reach it.

    Rebuilding a sequence as `type(value)(<generator>)` works for `list` and
    `tuple` and raises `TypeError` for every `namedtuple`, whose constructor takes
    one argument per field. That is not a redaction failure but a worse one: the
    exception escapes `RedactingFilter.filter` and takes down the log call, so the
    event is lost rather than logged unredacted.

    `property_trees` does not generate named tuples, so this is not reachable by
    the totality property above — it is here to stay reachable at all.
    """
    pair = namedtuple("pair", "left right")

    assert redact(pair(left={SEALED_KEY: SECRET_SENTINEL}, right=1)) == [REDACTED, 1]


@given(st.dictionaries(st.text(max_size=8), st.text(max_size=8), max_size=4))
def test_redaction_leaves_a_secret_free_payload_alone(payload: dict[str, str]) -> None:
    """No false positives. A redactor that ate ordinary config would push people
    to log around it, which is how the guarantee gets bypassed in practice."""
    assert redact(payload) == payload
    assert REDACTED not in _serialize(redact(payload))


# -- sealing -----------------------------------------------------------------


@given(st.text(max_size=200))
def test_sealing_and_unsealing_are_inverse(material: KeyMaterial, plaintext: str) -> None:
    """Includes the empty string and astral-plane characters, both of which are
    real (an empty secret is a misconfiguration worth surviving, and a UTF-8
    round trip through base64 is where a byte/str confusion shows up)."""
    marker = material.seal(plaintext)

    assert is_sealed_marker(marker)
    assert material.unseal(marker) == plaintext


@given(st.text(min_size=8, max_size=200))
def test_a_sealed_value_never_shows_its_plaintext(material: KeyMaterial, plaintext: str) -> None:
    """The ciphertext does not embed what it encrypts.

    Checked against the decoded bytes as well as the marker text, because the
    marker is base64 and a short plaintext collides with the base64 alphabet by
    chance — `"P"` appears in roughly one encoding in twenty for reasons that have
    nothing to do with the cipher. The eight-character floor is what makes the
    assertion mean "the cipher did not pass its input through" rather than "no
    byte coincided"; below it the property is noise in either direction.
    """
    marker = material.seal(plaintext)

    assert plaintext not in _serialize(marker)
    assert plaintext.encode("utf-8") not in b64decode(
        marker[SEALED_KEY].removeprefix(SEAL_V2_PREFIX)
    )


@given(st.text(min_size=1, max_size=100))
def test_sealing_the_same_value_twice_produces_different_ciphertext(
    material: KeyMaterial, plaintext: str
) -> None:
    """A fresh nonce per seal. A deterministic seal would leak equality: anyone
    reading state could tell which resources share a password without decrypting
    anything."""
    first, second = material.seal(plaintext), material.seal(plaintext)

    assert first != second
    assert material.unseal(first) == material.unseal(second) == plaintext


@given(st.binary(max_size=200))
def test_tampered_ciphertext_never_decrypts_to_a_plaintext(
    material: KeyMaterial, blob: bytes
) -> None:
    """Fail closed, always.

    The dangerous outcome is not an exception — it is a *value*. A caller handed a
    wrong plaintext has no way to know, and will go on to write it somewhere or
    compare it against something. The GCM tag is what makes refusal the only
    reachable outcome, and this asserts there is no path around it.
    """
    marker = {SEALED_KEY: b64encode(blob).decode("ascii")}

    with pytest.raises((SecretsError, ValueError, UnicodeDecodeError)):
        material.unseal(marker)


# -- digests -----------------------------------------------------------------


@given(st.text(max_size=100), st.text(max_size=20))
def test_a_digest_is_stable(plaintext: str, scope: str) -> None:
    """Rotation detection compares a stored digest against a freshly computed one,
    so an unstable digest reports every secret as rotated on every plan."""
    assert secret_digest(scope, plaintext) == secret_digest(scope, plaintext)


@given(st.text(min_size=8, max_size=100), st.text(max_size=20))
def test_a_digest_never_contains_its_plaintext(plaintext: str, scope: str) -> None:
    """The digest is stored in state, readable by anyone who can read state, so it
    must be a one-way function of the value.

    The eight-character floor is what gives the assertion content. A digest is 64
    hex characters, so a one-character plaintext like ``"0"`` appears inside one by
    chance more often than not — a shorter floor would make this fail for reasons
    that say nothing about whether the value was disclosed.
    """
    assert plaintext not in secret_digest(scope, plaintext)


@given(st.text(max_size=100), st.text(max_size=20), st.text(max_size=20))
def test_the_same_value_under_two_scopes_digests_differently(
    plaintext: str, one: str, other: str
) -> None:
    """The anti-correlation guarantee. Without per-scope scoping, equal digests
    across two fields would announce that they hold the same secret — a fact the
    state file is not supposed to carry."""
    assume(one != other)

    assert secret_digest(one, plaintext) != secret_digest(other, plaintext)


@given(st.text(max_size=100), st.text(max_size=100), st.text(max_size=20))
def test_two_different_values_digest_differently(first: str, second: str, scope: str) -> None:
    """Rotation detection is exactly this comparison; a collision here reads as
    "the secret did not change" and the resource is never updated."""
    assume(first != second)

    assert secret_digest(scope, first) != secret_digest(scope, second)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
