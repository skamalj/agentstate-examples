"""The consumer that enforces the deadline nobody else enforces.

`agent-wait` publishes `expires_at` and `default` on every envelope and acts on neither.
This is the other half of that bargain, written once: read the envelope, ask EventBridge
Scheduler to deliver the policy's own `default` to the agent's answers queue at
`expires_at`, and stop.

It is not on the agent's path and knows nothing about LangGraph. It reads a JSON document
off a queue and creates a one-shot schedule. The message it schedules is an ordinary
answer in the documented shape -- the same JSON a human would send by clicking reject:

    {"thread_id": ..., "question_id": ..., "answer": <the envelope's default>}

Nothing here waits. The schedule is a row in AWS, not a process; this function runs for a
moment at question time and is gone long before the deadline it just set.

Environment:
    REFUND_ANSWERS_QUEUE_ARN   where the schedule delivers (the agent's inbound FIFO queue)
    REFUND_ANSWERS_QUEUE_URL   used only for the send-now path
    REFUND_SCHEDULER_ROLE_ARN  the role EventBridge Scheduler assumes to send that message
    REFUND_SCHEDULE_DLQ_ARN    where a delivery Scheduler could not make is reported
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import boto3

_log = logging.getLogger("timeout_scheduler")
_log.setLevel(logging.INFO)

# Built on first use rather than at import, so the module can be imported (and tested)
# without a region or credentials. Lambda has both; a laptop running the tests need not.
_scheduler: Any = None
_sqs: Any = None


def _scheduler_client() -> Any:
    global _scheduler
    if _scheduler is None:
        _scheduler = boto3.client("scheduler")
    return _scheduler


def _sqs_client() -> Any:
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs

# Schedule names are limited to 64 characters. The prefix costs 16, so the question id is
# truncated to 24 -- still 96 bits of a hash, and the name only has to be unique among
# open questions, not globally.
NAME_PREFIX = "refund-timeout-"
ID_CHARS = 24


def schedule_name(question_id: str) -> str:
    return f"{NAME_PREFIX}{question_id[:ID_CHARS]}"


def answer_for(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The timeout message: the envelope's own `default`, in the documented answer shape.

    Nothing is invented here. `reply_with` is the stub agent-wait publishes for exactly
    this purpose; the deadline path fills it in with `default` instead of a human's click.
    """
    reply = dict(envelope["reply_with"])
    reply["answer"] = envelope.get("default")
    return reply


def _at_expression(expires_at: str) -> str:
    """`at(yyyy-mm-ddThh:mm:ss)`, which is the only one-shot form Scheduler takes."""
    return "at(" + expires_at.rstrip("Z") + ")"


def schedule_timeout(envelope: Mapping[str, Any]) -> str:
    """Create the one-shot schedule. Returns what happened, for the log and the tests."""
    expires_at = envelope.get("expires_at")
    if not expires_at:
        # A policy with no `timeout` publishes no deadline. There is nothing to enforce,
        # and that is the common case for anyone who never set one.
        _log.info(json.dumps({"question_id": envelope.get("question_id"), "action": "no_deadline"}))
        return "no_deadline"

    answer = answer_for(envelope)
    deadline = dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

    if deadline <= dt.datetime.now(dt.timezone.utc):
        # The deadline passed before we got here -- a redelivered envelope, or a queue
        # that was backed up. A schedule in the past is refused by Scheduler, so send the
        # default now. Late is the answer the asker asked for; a dropped message is not.
        _sqs_client().send_message(
            QueueUrl=os.environ["REFUND_ANSWERS_QUEUE_URL"],
            MessageBody=json.dumps(answer),
            MessageGroupId=str(answer["thread_id"]),
            MessageDeduplicationId=schedule_name(str(envelope["question_id"])),
        )
        _log.info(json.dumps({"question_id": envelope.get("question_id"), "action": "sent_now"}))
        return "sent_now"

    name = schedule_name(str(envelope["question_id"]))
    scheduler = _scheduler_client()

    # `DeadLetterConfig` is optional in the API, and the whole point of it here is to make
    # a delivery failure visible -- so when it is not configured, say so out loud. A
    # module that crashed on a missing optional variable would be worse than one without a
    # DLQ, and one that dropped it silently would reinvent the failure it exists to catch.
    target: dict[str, Any] = {
        "Arn": os.environ["REFUND_ANSWERS_QUEUE_ARN"],
        "RoleArn": os.environ["REFUND_SCHEDULER_ROLE_ARN"],
        "Input": json.dumps(answer),
        # The answers queue is FIFO, so the delivery needs a group -- the same thread id
        # every other message on that queue uses.
        "SqsParameters": {"MessageGroupId": str(answer["thread_id"])},
    }
    dlq_arn = os.environ.get("REFUND_SCHEDULE_DLQ_ARN")
    if dlq_arn:
        target["DeadLetterConfig"] = {"Arn": dlq_arn}
    else:
        _log.warning(
            "REFUND_SCHEDULE_DLQ_ARN is not set: a delivery EventBridge Scheduler cannot "
            "make will be lost silently. The answers queue's own DLQ cannot catch it."
        )

    try:
        scheduler.create_schedule(
            Name=name,
            ScheduleExpression=_at_expression(expires_at),
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            # The schedule reaps itself once it has fired, so nothing accumulates and
            # there is no sweeper to run. One question, one row, and then not even that.
            ActionAfterCompletion="DELETE",
            Target=target,
            Description=f"agent-wait timeout for {envelope['question_id']}",
        )
    except scheduler.exceptions.ConflictException:
        # The name is derived from the question id, so a republished envelope asks for a
        # schedule that already exists. That is the same identity `dedupe_key` uses, one
        # layer out: one question, one timer, however many times it is announced.
        _log.info(json.dumps({"question_id": envelope.get("question_id"), "action": "already_scheduled"}))
        return "already_scheduled"

    _log.info(json.dumps({"question_id": envelope.get("question_id"), "action": "scheduled", "at": expires_at}))
    return "scheduled"


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """SQS entry point. One envelope at a time, off the timeouts queue."""
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            schedule_timeout(json.loads(record["body"]))
        except Exception:
            _log.exception("failed on message %s", record.get("messageId"))
            failures.append({"itemIdentifier": str(record.get("messageId", ""))})
    return {"batchItemFailures": failures}
