"""Act 4, driven from a laptop against the deployed stack.

Plays the customer and then finance, against the real queues:

    send a start to answers.fifo   -> Lambda runs, parks, publishes
    read questions.fifo            -> the envelope, from a process that never ran the graph
    send the answer to answers.fifo-> a second Lambda resumes the thread and refunds
    send it twice more             -> nothing runs

Nothing here imports the agent. Resource identifiers come from the stack outputs, and
only names are printed -- never an account id.

    uv run python deployed_run.py            # after `npx aws-cdk deploy`
"""

from __future__ import annotations

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
    sys.exit(main())
