"""CLI never renders a secret — ``$secret_ref`` handles redact to ``(sensitive)``."""

from __future__ import annotations

from atlantide.cli.views.common import SECRET_REDACTED, fmt_value
from atlantide.cli.views.plan import field_diffs
from atlantide.cli.views.report import report_json
from atlantide.core import SecretRef
from atlantide.ir.model import IRNode
from atlantide.providers import random as random_provider
from atlantide.providers.random import RandomProvider
from atlantide.reconcile import Action, ApplyReport, Change
from atlantide.state import StateNode
from tests.conftest import make_engine

_MARKER = SecretRef("app/signing-key").canonical()


def test_fmt_value_redacts_secret_ref_marker() -> None:
    assert fmt_value(_MARKER) == SECRET_REDACTED
    assert fmt_value("plain") == "'plain'"  # ordinary values unaffected


def test_field_diffs_redact_rotated_secret() -> None:
    node_id = "default:mock.Vault:v"
    prior = StateNode(
        id=node_id,
        type="mock.Vault",
        provider="mock",
        provider_version="1.0.0",
        input_hash="h",
        properties={"token": _MARKER},
    )
    desired = IRNode(
        id=node_id,
        type="mock.Vault",
        provider="mock",
        provider_version="1.0.0",
        properties={"token": _MARKER},
        dependencies=(),
    )
    change = Change(
        node_id=node_id,
        action=Action.UPDATE,
        desired=desired,
        prior=prior,
        changed_fields=("token",),
    )
    lines = field_diffs(change)
    assert lines == [f"token: {SECRET_REDACTED} → {SECRET_REDACTED}"]
    assert "app/signing-key" not in lines[0]  # the handle name does not leak either


def test_report_json_redacts_secret_output() -> None:
    report = ApplyReport(outputs={"pw": _MARKER, "note": "v1"})
    assert report_json(report)["outputs"] == {"pw": None, "note": "v1"}


def test_report_json_redacts_sensitive_computed_output() -> None:
    # A generated secret (e.g. random.Password.result) is plaintext in outputs;
    # the executor marks its declared-output name so renderers redact it.
    report = ApplyReport(
        outputs={"default:pw": "hunter2", "default:note": "v1"},
        sensitive_outputs=frozenset({"default:pw"}),
    )
    assert report_json(report)["outputs"] == {"default:pw": None, "default:note": "v1"}


async def test_apply_marks_generated_password_output_sensitive() -> None:
    """End-to-end: output(...) of a Password.result never reaches renderers unredacted."""
    engine = make_engine(random_provider.TYPES, RandomProvider())
    src = (
        "from atlantide.providers.random import Password\n"
        "from atlantide.core import output\n"
        "p = Password('p', length=12)\n"
        "output('pw', p.result)\n"
    )
    report = (await engine.apply(src)).unwrap()
    generated = report.outputs["default:pw"]
    assert isinstance(generated, str) and len(generated) == 12  # resolved for machines
    assert "default:pw" in report.sensitive_outputs
    assert report_json(report)["outputs"]["default:pw"] is None  # never emitted
