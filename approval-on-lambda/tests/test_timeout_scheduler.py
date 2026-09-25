"""The consumer that enforces `expires_at`, tested without AWS.

moto ships an EventBridge Scheduler backend, so the schedule this function creates is a
real object we can read back and assert on: the expression, the target, the group id and
the payload. What moto does not do is *fire* a schedule — that half is only provable on
the deployed stack, and `run-deployed-timeout.log` is where it is proved.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import timeout_scheduler  # noqa: E402

QUEUE_ARN = "arn:aws:sqs:ap-south-1:123456789012:answers.fifo"
ROLE_ARN = "arn:aws:iam::123456789012:role/scheduler"
DLQ_ARN = "arn:aws:sqs:ap-south-1:123456789012:schedule-dlq"


def envelope(expires_at, question_id=None, thread_id="order-4471"):
    """The shape agent-wait publishes, trimmed to what this consumer reads."""
    return {
        "type": "wait.created",
        "thread_id": thread_id,
        "question_id": question_id or uuid.uuid4().hex,
        "question": {"function": "escalate_refund", "args": {"order_id": thread_id, "amount": 41000}},
        "expires_at": expires_at,
        "default": {"action": "reject", "reason": "no finance response within PT2M"},
        "reply_with": {"thread_id": thread_id, "question_id": question_id or "x", "answer": None},
    }


@pytest.fixture
def scheduler(monkeypatch):
    """A moto EventBridge Scheduler, and nothing else.

    Deliberately independent of the `local` fixture and of any real credentials: this
    whole file runs with no AWS account, which is the point of testing the consumer
    rather than the cloud.
    """
    from moto import mock_aws

    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "ap-south-1",
        "REFUND_ANSWERS_QUEUE_ARN": QUEUE_ARN,
        "REFUND_SCHEDULER_ROLE_ARN": ROLE_ARN,
        "REFUND_SCHEDULE_DLQ_ARN": DLQ_ARN,
    }.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    for var in ("AWS_ENDPOINT_URL_DYNAMODB", "AWS_ENDPOINT_URL_SQS"):
        monkeypatch.delenv(var, raising=False)

    with mock_aws():
        client = boto3.client("scheduler", region_name="ap-south-1")
        monkeypatch.setattr(timeout_scheduler, "_scheduler", client)
        yield client


def test_it_schedules_the_default_at_the_deadline(scheduler):
    """The whole mechanism: the envelope's own default, delivered at expires_at."""
    env = envelope("2030-01-01T09:00:00Z")
    env["reply_with"]["question_id"] = env["question_id"]

    assert timeout_scheduler.schedule_timeout(env) == "scheduled"

    got = scheduler.get_schedule(Name=timeout_scheduler.schedule_name(env["question_id"]))
    assert got["ScheduleExpression"] == "at(2030-01-01T09:00:00)"
    assert got["ScheduleExpressionTimezone"] == "UTC"
    assert got["FlexibleTimeWindow"]["Mode"] == "OFF"
    # the schedule reaps itself, so nothing accumulates and there is no sweeper
    assert got["ActionAfterCompletion"] == "DELETE"

    target = got["Target"]
    assert target["Arn"] == QUEUE_ARN
    assert target["RoleArn"] == ROLE_ARN
    assert target["SqsParameters"]["MessageGroupId"] == env["thread_id"]
    # the only thing that would report a delivery Scheduler could not make: the answers
    # queue's own DLQ cannot, because a rejected send never enters the queue
    assert target["DeadLetterConfig"]["Arn"] == DLQ_ARN

    # the payload is an ordinary answer in the documented shape -- nothing invented
    assert json.loads(target["Input"]) == {
        "thread_id": env["thread_id"],
        "question_id": env["question_id"],
        "answer": env["default"],
    }


def test_a_republished_envelope_does_not_create_a_second_timer(scheduler):
    """The name is derived from question_id, so one question gets one timer."""
    env = envelope("2030-01-01T09:00:00Z")
    env["reply_with"]["question_id"] = env["question_id"]

    assert timeout_scheduler.schedule_timeout(env) == "scheduled"
    assert timeout_scheduler.schedule_timeout(env) == "already_scheduled"

    names = [s["Name"] for s in scheduler.list_schedules()["Schedules"]]
    assert names.count(timeout_scheduler.schedule_name(env["question_id"])) == 1


def test_a_policy_with_no_timeout_is_skipped(scheduler):
    """WaitPolicy() with no timeout publishes expires_at: null. Nothing to enforce."""
    env = envelope(None)

    assert timeout_scheduler.schedule_timeout(env) == "no_deadline"
    assert scheduler.list_schedules()["Schedules"] == []


def test_a_deadline_already_past_is_sent_now(scheduler, monkeypatch):
    """A delayed or redelivered envelope must not produce a schedule in the past."""
    sent = {}

    class _Sqs:
        def send_message(self, **kw):
            sent.update(kw)

    monkeypatch.setattr(timeout_scheduler, "_sqs", _Sqs())
    monkeypatch.setenv("REFUND_ANSWERS_QUEUE_URL", "https://sqs.invalid/answers.fifo")
    env = envelope("2020-01-01T09:00:00Z")
    env["reply_with"]["question_id"] = env["question_id"]

    assert timeout_scheduler.schedule_timeout(env) == "sent_now"
    assert scheduler.list_schedules()["Schedules"] == []
    assert json.loads(sent["MessageBody"])["answer"] == env["default"]
    assert sent["MessageGroupId"] == env["thread_id"]


def test_the_schedule_name_fits_the_limit(scheduler):
    """Schedule names are capped at 64 characters; the id is truncated to fit."""
    name = timeout_scheduler.schedule_name("f" * 64)
    assert len(name) <= 64
    assert name.startswith("refund-timeout-")


def test_without_a_dead_letter_queue_it_still_schedules_and_says_so(scheduler, monkeypatch, caplog):
    """`DeadLetterConfig` is optional, so a missing arn must not crash the consumer.

    It must also not be dropped silently: the DLQ exists to make an invisible failure
    visible, and omitting it without a word would reinvent that failure one level up.
    """
    import logging

    monkeypatch.delenv("REFUND_SCHEDULE_DLQ_ARN")
    env = envelope("2030-01-01T09:00:00Z")
    env["reply_with"]["question_id"] = env["question_id"]

    with caplog.at_level(logging.WARNING, logger="timeout_scheduler"):
        assert timeout_scheduler.schedule_timeout(env) == "scheduled"

    got = scheduler.get_schedule(Name=timeout_scheduler.schedule_name(env["question_id"]))
    assert "DeadLetterConfig" not in got["Target"]
    assert "REFUND_SCHEDULE_DLQ_ARN is not set" in caplog.text
