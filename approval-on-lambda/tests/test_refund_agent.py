"""The post's claims, as assertions.

Every test runs the real agent against a real model, parks it, kills the interpreter and
answers from another one. DynamoDB and SQS are `moto_server`; the side effect is counted
in a table so "it ran once" is a fact outside any process.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from conftest import answer, approvals_row, calls, envelope_for, pids, start

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import graph as refund  # noqa: E402


@pytest.fixture(scope="module")
def approved(local):
    """One thread: parked, then answered three times -- approve, approve, reject."""
    order_id = f"order-{uuid.uuid4().hex[:8]}"
    out = {"start": start(local, order_id)}
    out["envelope"] = envelope_for(local, order_id)
    approve = {"action": "approve", "by": "finance", "note": "vendor confirmed non-delivery"}
    out["first"] = answer(local, out["envelope"], approve)
    out["duplicate"] = answer(local, out["envelope"], approve)
    out["late_reject"] = answer(local, out["envelope"], {"action": "reject", "reason": "changed my mind"})
    out["calls"] = calls(local, order_id)
    out["order_id"] = order_id
    return out


def test_the_run_parks_instead_of_refunding(approved):
    assert approved["envelope"]["question"]["function"] == "escalate_refund"
    # nothing was refunded while the question was open
    assert "refunds issued this process: []" in approved["start"]


def test_a_fresh_process_resumes_the_thread(approved):
    """The park and the resume are different interpreters, and the refund ran in the second."""
    parked_pid = pids(approved["start"])[0]
    resumed_pid = pids(approved["first"])[0]
    assert parked_pid != resumed_pid
    assert f"refunds issued this process: ['{approved['order_id']}']" in approved["first"]


def test_the_refund_runs_exactly_once(approved):
    assert approved["calls"] == 1


def test_a_duplicate_answer_runs_nothing(approved):
    assert "refunds issued this process: []" in approved["duplicate"]
    assert "refunds issued this process: []" in approved["late_reject"]


def test_the_envelope_carries_the_documented_fields(approved):
    envelope = approved["envelope"]
    assert envelope["type"] == "wait.created"
    assert envelope["question"] == {
        "function": "escalate_refund",
        "args": {"order_id": approved["order_id"], "amount": 41000},
    }
    assert envelope["allowed_actions"] == ["approve", "reject"]
    assert envelope["tags"] == {"approver_group": "finance"}
    assert envelope["default"]["action"] == "reject"
    assert envelope["source"] == {"function": "escalate_refund"}
    assert envelope["reply_with"] == {
        "thread_id": approved["order_id"],
        "question_id": envelope["question_id"],
        "answer": None,
    }
    assert envelope["expires_at"].endswith("Z")


def test_the_question_is_also_a_row(approved, local):
    row = approvals_row(local, approved["order_id"], approved["envelope"]["question_id"])
    assert row["status"] == "open"  # the adapter writes it and never touches it again
    assert row["expires_at"] == approved["envelope"]["expires_at"]
    assert row["reply_with"]["question_id"] == approved["envelope"]["question_id"]


def test_the_deadline_default_is_an_ordinary_answer(local, order_id):
    """Nobody enforces expires_at. The consumer sends the policy's own default instead."""
    start(local, order_id)
    envelope = envelope_for(local, order_id)
    out = answer(local, envelope, envelope["default"])
    assert "refunds issued this process: []" in out
    assert calls(local, order_id) == 0


def test_under_the_limit_nothing_is_asked(local, order_id):
    from act3_asks import run_host

    out = run_host(
        {"thread_id": order_id, "input": {"messages": [("user", refund.under_limit(order_id, 1_800))]}},
        local["env"],
    )
    assert f"refunds issued this process: ['{order_id}']" in out
    assert calls(local, order_id) == 1


def test_ttl_seconds_stamps_every_parked_item(local, order_id, tmp_path):
    """`DynamoDBSaver(ttl_seconds=...)` puts a ttl on the checkpoint and on every write."""
    import boto3
    from langgraph_dynamodb_checkpoint import DynamoDBSaver

    table_name = f"ttl-{order_id}"
    saver = DynamoDBSaver(table_name, ttl_seconds=24 * 60 * 60)
    agent = refund.build_agent(saver)
    result = agent.invoke(
        {"messages": [("user", refund.over_limit(order_id, 41_000))]},
        {"configurable": {"thread_id": order_id}},
    )
    assert result.get("__interrupt__")

    items = boto3.resource("dynamodb").Table(table_name).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(order_id),
        ConsistentRead=True,
    )["Items"]
    assert items
    assert all("ttl" in item for item in items)
    spec = boto3.client("dynamodb").describe_time_to_live(TableName=table_name)
    # the package enables table TTL only on a table it created itself -- this is one
    assert spec["TimeToLiveDescription"]["AttributeName"] == "ttl"
    assert spec["TimeToLiveDescription"]["TimeToLiveStatus"] == "ENABLED"


def test_the_limit_is_code_not_a_prompt(local, order_id, monkeypatch):
    """The model picks the tool; the tool enforces the limit.

    Call the unguarded-looking tool directly with an amount only finance may approve. It
    must refuse without refunding, whatever the system prompt says.
    """
    import act3_asks

    monkeypatch.setenv("REFUND_SIDE_EFFECT_TABLE", act3_asks.SIDE_EFFECTS)
    refund.reset()

    out = refund.issue_refund.invoke({"order_id": order_id, "amount": 41_000})

    assert "refused" in out and "escalate_refund" in out
    assert refund.REFUNDS_ISSUED == []
    assert calls(local, order_id) == 0

    # and the same tool still works under the limit, with the counter to prove it
    assert refund.issue_refund.invoke({"order_id": order_id, "amount": 1_800}).startswith("refunded")
    assert calls(local, order_id) == 1
