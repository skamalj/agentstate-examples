"""Act 4, driven from a laptop against the deployed stack.

Plays the customer and then finance, against the real queues:

    send a start to answers.fifo   -> Lambda runs, parks, publishes
    read questions.fifo            -> the envelope, from a process that never ran the graph
    send the answer to answers.fifo-> a second Lambda resumes the thread and refunds
    send it twice more             -> nothing runs

Nothing here imports the agent. Resource identifiers come from the stack outputs, and
only names are printed -- never an account id.

    uv run python deployed_run.py            # the approve path
    uv run python deployed_run.py --timeout  # nobody answers; the deadline does
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time
import uuid

import boto3

sys.stdout.reconfigure(encoding="utf-8")

OUTPUTS = pathlib.Path(__file__).parent / "cdk" / "outputs.json"


def outputs() -> dict[str, str]:
    data = json.loads(OUTPUTS.read_text())
    return next(iter(data.values()))


def name_of(queue_url: str) -> str:
    return queue_url.rsplit("/", 1)[-1]


def send(sqs, url: str, message: dict) -> None:
    sqs.send_message(
        QueueUrl=url,
        MessageBody=json.dumps(message),
        MessageGroupId=str(message["thread_id"]),  # one thread, one order of processing
        MessageDeduplicationId=uuid.uuid4().hex,
    )


def poll(sqs, url: str, thread_id: str, seconds: int = 90) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=5)
        for raw in got.get("Messages", []):
            body = json.loads(raw["Body"])
            sqs.delete_message(QueueUrl=url, ReceiptHandle=raw["ReceiptHandle"])
            if body.get("thread_id") == thread_id:
                return body
    return None


def refunds(ddb, table: str, order_id: str) -> int:
    item = ddb.Table(table).get_item(Key={"pk": f"REFUND#{order_id}", "sk": "#"}, ConsistentRead=True).get("Item")
    return int(item["calls"]) if item else 0


def open_questions(ddb, table: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    return ddb.Table(table).query(
        IndexName="by_status", KeyConditionExpression=Key("status").eq("open")
    )["Items"]


def resume_event(out: dict, thread_id: str, seconds: int = 120) -> dt.datetime | None:
    """When the run function logged an `answer` for this thread, by AWS's clock."""
    logs = boto3.client("logs")
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = logs.filter_log_events(
            logGroupName=out["LogGroup"],
            filterPattern='{ $.kind = "answer" }',
            startTime=int((time.time() - 3600) * 1000),
        )
        for e in found.get("events", []):
            if thread_id in e["message"]:
                return dt.datetime.fromtimestamp(e["timestamp"] / 1000, dt.timezone.utc)
        time.sleep(5)
    return None


def open_for(ddb, out: dict, thread_id: str) -> list[dict]:
    return [r for r in open_questions(ddb, out["ApprovalsTable"]) if r["thread_id"] == thread_id]


def timeout_run() -> int:
    """Nobody answers. A consumer turns `expires_at` into a schedule, and the schedule answers.

    agent-wait is not involved past publishing the envelope. The deadline is enforced by
    a scheduler Lambda, an EventBridge one-shot schedule and an IAM role -- no human, no
    agent code, and nothing running while it waits.
    """
    out = outputs()
    sqs, ddb = boto3.client("sqs"), boto3.resource("dynamodb")
    scheduler = boto3.client("scheduler")
    answers, questions = out["AnswersQueueUrl"], out["QuestionsQueueUrl"]
    order_id = f"order-{uuid.uuid4().hex[:6]}"

    print(f"run function      : {out['FunctionName']}")
    print(f"scheduler function: {out['SchedulerFunctionName']}")
    print(f"order             : {order_id}")

    started = time.time()
    send(sqs, answers, {
        "thread_id": order_id,
        "input": {"messages": [("user", f"Please refund order {order_id}, 41000 rupees, the laptop was never delivered.")]},
    })
    print("\n--- the customer asks, and nobody is going to answer ---")

    envelope = poll(sqs, questions, order_id)
    assert envelope, "no question arrived on the questions queue"
    expires_at = envelope["expires_at"]
    print(f"question arrived after {time.time() - started:.1f}s")
    print(f"question_id : {envelope['question_id']}")
    print(f"expires_at  : {expires_at}")
    print(f"default     : {json.dumps(envelope['default'])}")

    # The same envelope also went to the timeouts queue, where the scheduler Lambda read
    # it. Nothing told it to; it is a second consumer of one publish_interrupts call.
    name = "refund-timeout-" + envelope["question_id"][:24]
    schedule = None
    for _ in range(30):
        try:
            schedule = scheduler.get_schedule(Name=name)
            break
        except scheduler.exceptions.ResourceNotFoundException:
            time.sleep(2)
    assert schedule, f"the consumer created no schedule named {name}"

    print("\n--- what the consumer created, before it fires ---")
    print(json.dumps({
        "Name": schedule["Name"],
        "ScheduleExpression": schedule["ScheduleExpression"],
        "ScheduleExpressionTimezone": schedule["ScheduleExpressionTimezone"],
        "ActionAfterCompletion": schedule["ActionAfterCompletion"],
        "Target": {
            # an arn is colon-separated and carries the account id; print the tail only
            "Arn": "...:" + schedule["Target"]["Arn"].rsplit(":", 1)[-1],
            "SqsParameters": schedule["Target"]["SqsParameters"],
            "Input": json.loads(schedule["Target"]["Input"]),
        },
    }, indent=2))
    print(f"\nrefunds recorded while it waits: {refunds(ddb, out['SideEffectTable'], order_id)}")
    print("open rows for this thread       : " + str(len(open_for(ddb, out, order_id))))

    deadline = dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    wait_s = max(0.0, (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds())
    print(f"\n--- waiting {wait_s:.0f}s for the deadline; no process of ours is running ---")

    gone = False
    for _ in range(120):
        time.sleep(5)
        try:
            scheduler.get_schedule(Name=name)
        except scheduler.exceptions.ResourceNotFoundException:
            gone = True
            break
    assert gone, "the schedule never fired"
    print("the schedule fired and deleted itself (ActionAfterCompletion=DELETE)")

    # Wait for the agent to actually resume, and time it off CloudWatch rather than this
    # laptop: the local clock here is over a minute behind AWS, and a lateness measured
    # against a skewed clock would be fiction.
    resumed = resume_event(out, order_id, seconds=180)
    assert resumed, "the deadline fired but the agent never resumed"
    print(f"the agent resumed at {resumed.strftime('%H:%M:%SZ')},"
          f" {(resumed - deadline).total_seconds():.0f}s after expires_at")
    print("(EventBridge Scheduler is minute-granular; that gap is delivery, not drift)")

    print(f"\nrefunds recorded after the deadline: {refunds(ddb, out['SideEffectTable'], order_id)}")

    print("\n--- a human approves, too late ---")
    send(sqs, answers, dict(envelope["reply_with"], answer={"action": "approve", "by": "finance"}))
    time.sleep(25)
    print(f"refunds recorded after the late approval: {refunds(ddb, out['SideEffectTable'], order_id)}")
    print("nothing ran, for the same reason a duplicate approval runs nothing:")
    print("the thread has already moved past the question")

    # If Scheduler could not deliver, this is where it says so -- and it is the only
    # place that would. A rejected send never enters the answers queue, so that queue's
    # own dead-letter queue would have stayed empty and told us nothing.
    depth = sqs.get_queue_attributes(
        QueueUrl=out["ScheduleDlqUrl"], AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]["ApproximateNumberOfMessages"]
    print(f"\nundelivered schedules on the schedule dead-letter queue: {depth}")

    print(f"\nthread_id = {order_id}")
    print(f"question_id = {envelope['question_id']}")
    return 0


def main() -> int:
    out = outputs()
    sqs, ddb = boto3.client("sqs"), boto3.resource("dynamodb")
    answers, questions = out["AnswersQueueUrl"], out["QuestionsQueueUrl"]
    order_id = f"order-{uuid.uuid4().hex[:6]}"

    print(f"function      : {out['FunctionName']}")
    print(f"answers queue : {name_of(answers)}")
    print(f"questions queue: {name_of(questions)}")
    print(f"order         : {order_id}\n")

    started = time.time()
    send(sqs, answers, {
        "thread_id": order_id,
        "input": {"messages": [("user", f"Please refund order {order_id}, 41000 rupees, the laptop was never delivered.")]},
    })
    print("--- the customer's message is on the queue; nothing of ours is running ---")

    envelope = poll(sqs, questions, order_id)
    assert envelope, "no question arrived on the questions queue"
    print(f"question arrived after {time.time() - started:.1f}s")
    print(json.dumps(envelope, indent=2))

    rows = open_questions(ddb, out["ApprovalsTable"])
    print(f"\nopen questions in the approvals table (GSI by_status): {len(rows)}")
    print(f"refunds recorded so far: {refunds(ddb, out['SideEffectTable'], order_id)}")

    print("\n--- finance approves, three days later as far as anything here knows ---")
    answer = dict(envelope["reply_with"], answer={"action": "approve", "by": "finance"})
    send(sqs, answers, answer)
    for _ in range(30):
        time.sleep(2)
        if refunds(ddb, out["SideEffectTable"], order_id):
            break
    print(f"refunds recorded after the approval: {refunds(ddb, out['SideEffectTable'], order_id)}")

    # The row the approvals UI reads is still "open", because nothing closes it. The
    # adapter writes it once and never touches it again; marking it answered is the
    # host's job, and this host does not do it.
    still_open = [r for r in open_questions(ddb, out["ApprovalsTable"]) if r["thread_id"] == order_id]
    print(f"rows for this thread still status=open after the refund: {len(still_open)}")

    print("\n--- the same answer twice more ---")
    send(sqs, answers, answer)
    send(sqs, answers, dict(answer, answer={"action": "reject", "reason": "too late"}))
    time.sleep(25)
    print(f"refunds recorded after three answers in total: {refunds(ddb, out['SideEffectTable'], order_id)}")

    left = poll(sqs, questions, order_id, seconds=5)
    print(f"further questions published for this thread: {0 if left is None else 1}")
    print(f"\nthread_id = {order_id}")
    print(f"question_id = {envelope['question_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(timeout_run() if "--timeout" in sys.argv else main())
