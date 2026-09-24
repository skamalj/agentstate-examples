"""One local AWS, one set of tables and queues, shared by every test in the module.

Each scenario runs the host in child interpreters, exactly as the acts do, so
"a fresh process resumed it" is a fact about processes and not about objects.

Needs AWS credentials for Bedrock (the model is real); everything else is `moto_server`.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import act3_asks  # noqa: E402
from local_aws import LocalAws  # noqa: E402


def _aws_ok() -> bool:
    try:
        boto3.client("sts").get_caller_identity()
        return True
    except Exception:
        return False


collect_ignore_glob = [] if _aws_ok() else ["test_*.py"]


@pytest.fixture(scope="session")
def local():
    with LocalAws() as running:
        ddb = boto3.resource("dynamodb")
        sqs = boto3.client("sqs")
        act3_asks.make_tables(ddb)
        questions_url, answers_url = act3_asks.make_queues(sqs)
        env = dict(
            running.child_env,
            REFUND_QUESTIONS_QUEUE=questions_url,
            REFUND_SIDE_EFFECT_TABLE=act3_asks.SIDE_EFFECTS,
        )
        yield {
            "env": env,
            "ddb": ddb,
            "sqs": sqs,
            "questions_url": questions_url,
            "answers_url": answers_url,
        }


@pytest.fixture
def order_id() -> str:
    """A fresh order per test, so the side-effect counter is not shared."""
    return f"order-{uuid.uuid4().hex[:8]}"


def start(local, order_id: str, amount: int = 41_000) -> str:
    """Start a thread named after the order. thread_id == order_id here; they need not be."""
    import graph as refund

    return act3_asks.run_host(
        {
            "thread_id": order_id,
            "input": {"messages": [("user", refund.over_limit(order_id, amount))]},
        },
        local["env"],
    )


def answer(local, envelope: dict, value) -> str:
    message = dict(envelope["reply_with"], answer=value)
    return act3_asks.run_host(message, local["env"])


def envelope_for(local, thread_id: str) -> dict:
    """The envelope SqsAnnounce put on the questions queue for this thread."""
    for _ in range(5):
        for body in act3_asks.receive(local["sqs"], local["questions_url"]):
            if body["thread_id"] == thread_id:
                return body
    raise AssertionError(f"no envelope for {thread_id}")


def calls(local, order_id: str) -> int:
    return act3_asks.calls_recorded(local["ddb"], order_id)


def pids(output: str) -> list[str]:
    return [line.split("[pid ", 1)[1].split("]", 1)[0] for line in output.splitlines() if "[pid " in line]


def approvals_row(local, thread_id: str, question_id: str) -> dict:
    return local["ddb"].Table(act3_asks.APPROVALS).get_item(
        Key={"pk": f"THREAD#{thread_id}", "sk": f"WAIT#{question_id}"}, ConsistentRead=True
    )["Item"]


__all__ = ["answer", "approvals_row", "calls", "envelope_for", "json", "pids", "start"]
