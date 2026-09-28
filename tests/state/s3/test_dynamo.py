"""The DynamoDB request builders, pinned to their exact request shapes."""

from __future__ import annotations

from atlantide.state.s3.dynamo import ddb_num, ddb_str, head_update

_SET = "SET #s = :seq, #r = :ref, #op = :op, state_ns = :ns"
_NAMES = {"#s": "seq", "#r": "ref", "#op": "op"}


def test_a_leased_commit() -> None:
    assert head_update(
        "heads", "k", seq=2, ref="r", op="put", namespace="ns", expect=1, fence=7
    ) == {
        "TableName": "heads",
        "Key": {"node_id": {"S": "k"}},
        "UpdateExpression": _SET,
        "ConditionExpression": "(#s = :expect OR attribute_not_exists(#s)) AND fence = :f",
        "ExpressionAttributeNames": _NAMES,
        "ExpressionAttributeValues": {
            ":expect": {"N": "1"},
            ":seq": {"N": "2"},
            ":ref": {"S": "r"},
            ":op": {"S": "put"},
            ":ns": {"S": "ns"},
            ":f": {"N": "7"},
        },
    }


def test_an_unfenced_commit_as_a_transaction_item() -> None:
    update = head_update("heads", "k", seq=2, ref="r", op="delete", namespace="ns", expect=1)
    assert update["ConditionExpression"] == "(#s = :expect OR attribute_not_exists(#s))"
    assert ":f" not in update["ExpressionAttributeValues"]


def test_a_rebuilt_head_keeps_any_fence_it_has() -> None:
    update = head_update("heads", "k", seq=4, ref="r", op="put", namespace="ns", seed_fence=5)
    assert update["UpdateExpression"] == f"{_SET}, fence = if_not_exists(fence, :f)"
    assert update["ConditionExpression"] == "attribute_not_exists(#s)"
    assert update["ExpressionAttributeValues"] == {
        ":seq": {"N": "4"},
        ":ref": {"S": "r"},
        ":op": {"S": "put"},
        ":ns": {"S": "ns"},
        ":f": {"N": "5"},
    }


def test_numbers_keep_their_precision() -> None:
    assert ddb_num(0.1 + 0.2) == {"N": "0.30000000000000004"}
    assert ddb_num(3) == {"N": "3"}
    assert ddb_str("x") == {"S": "x"}
