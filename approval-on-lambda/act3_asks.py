"""Act 3. The question gets out, and the answer gets back in.

After act 2 the thread survives, but only the checkpoint knows a question was asked. This
act adds the two lines that tell somebody else:

    @wait(FINANCE)                          # already on the tool, in graph.py
    publish_interrupts(result, thread_id, announce=[SqsAnnounce(...), DynamoDbAnnounce(...)])

The parent plays the consumer: it reads the envelope off the questions queue, prints it,
prints the approvals row, and sends an answer to the answers queue. Child interpreters
play the host -- one per message, exactly as a Lambda would be.

    uv run python act3_asks.py            # moto_server, nothing in your account
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import boto3

from local_aws import LocalAws

sys.stdout.reconfigure(encoding="utf-8")

TABLE = os.environ.get("REFUND_CHECKPOINT_TABLE", "refund-checkpoints")
APPROVALS = os.environ.get("REFUND_APPROVALS_TABLE", "refund-approvals")
SIDE_EFFECTS = os.environ.get("REFUND_SIDE_EFFECT_TABLE", "refund-side-effects")


# --------------------------------------------------------------------------- the child
def host(message: dict) -> None:
    """The whole host. One graph, one `if`, one publish. This is `handler.py` without Lambda."""
    from langgraph.types import Command
    from langgraph_dynamodb_checkpoint import DynamoDBSaver

    from agent_wait.aws import DynamoDbAnnounce, SqsAnnounce
    from agent_wait.langgraph import publish_interrupts

    import graph as refund

    agent = refund.build_agent(DynamoDBSaver(TABLE))
    announce = [
        SqsAnnounce(os.environ["REFUND_QUESTIONS_QUEUE"]),
        DynamoDbAnnounce(APPROVALS),
    ]

    thread_id = str(message["thread_id"])
    config = {"configurable": {"thread_id": thread_id}}

    if "question_id" in message:  # an answer
        value = Command(resume={str(message["question_id"]): message.get("answer")})
    else:  # a start
        value = message.get("input")

    result = agent.invoke(value, config)
    publish_interrupts(result, thread_id, announce)
    print(f"  [pid {os.getpid()}] refunds issued this process: {refund.REFUNDS_ISSUED}")
    if not result.get("__interrupt__"):
        print(f"  [pid {os.getpid()}] agent says: {result['messages'][-1].text.strip()}")


# -------------------------------------------------------------------------- the parent
def run_host(message: dict, env: dict[str, str]) -> str:
    out = subprocess.run(
        [sys.executable, __file__, json.dumps(message)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sys.stdout.write(out.stdout)
    if out.returncode:
        sys.stdout.write(out.stderr)
        raise SystemExit(f"host exited {out.returncode}")
    return out.stdout


def make_tables(ddb) -> None:
    """The two tables the checkpointer does not create for you.

    `DynamoDBSaver` makes its own; `DynamoDbAnnounce` and the side-effect counter do not.
    The CDK stack creates all three when deployed.
    """
    for name in (APPROVALS, SIDE_EFFECTS):
        try:
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            ).wait_until_exists()
        except ddb.meta.client.exceptions.ResourceInUseException:
            pass


def calls_recorded(ddb, order_id: str) -> int:
    item = ddb.Table(SIDE_EFFECTS).get_item(
        Key={"pk": f"REFUND#{order_id}", "sk": "#"}, ConsistentRead=True
    ).get("Item")
    return int(item["calls"]) if item else 0


def make_queues(sqs) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    urls = []
    for name in ("refund-questions", "refund-answers"):
        urls.append(
            sqs.create_queue(
                QueueName=f"{name}-{suffix}.fifo",
                Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
            )["QueueUrl"]
        )
    return urls[0], urls[1]


def receive(sqs, url: str) -> list[dict]:
    got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
    out = []
    for raw in got.get("Messages", []):
        out.append(json.loads(raw["Body"]))
        sqs.delete_message(QueueUrl=url, ReceiptHandle=raw["ReceiptHandle"])
    return out


def main() -> int:
    thread_id = os.environ.get("REFUND_THREAD_ID") or f"order-4471-{uuid.uuid4().hex[:8]}"
    with LocalAws() as local:
        sqs = boto3.client("sqs")
        ddb = boto3.resource("dynamodb")
        make_tables(ddb)
        questions_url, answers_url = make_queues(sqs)
        env = dict(
            local.child_env,
            REFUND_QUESTIONS_QUEUE=questions_url,
            REFUND_SIDE_EFFECT_TABLE=SIDE_EFFECTS,
        )
        print(f"thread_id = {thread_id}")
        print(f"questions = {questions_url.rsplit('/', 1)[-1]}")
        print(f"answers   = {answers_url.rsplit('/', 1)[-1]}")

        print("\n--- host run 1: the customer asks ---")
        run_host({"thread_id": thread_id, "input": {"messages": [("user", graph_over_limit())]}}, env)

        print("\n--- what landed on the questions queue ---")
        envelopes = receive(sqs, questions_url)
        print(f"{len(envelopes)} message(s)")
        envelope = envelopes[0]
        print(json.dumps(envelope, indent=2))

        print("\n--- what landed in the approvals table ---")
        row = boto3.resource("dynamodb").Table(APPROVALS).get_item(
            Key={"pk": f"THREAD#{thread_id}", "sk": f"WAIT#{envelope['question_id']}"},
            ConsistentRead=True,
        )["Item"]
        print(json.dumps({k: row[k] for k in sorted(row)}, indent=2, default=_plain))

        print("\n--- finance clicks approve; the consumer fills in reply_with ---")
        answer = dict(envelope["reply_with"])
        answer["answer"] = {"action": "approve", "by": "finance", "note": "vendor confirmed non-delivery"}
        print(json.dumps(answer, indent=2))
        sqs.send_message(
            QueueUrl=answers_url,
            MessageBody=json.dumps(answer),
            MessageGroupId=answer["thread_id"],
            MessageDeduplicationId=uuid.uuid4().hex,
        )

        print("\n--- host run 2: the answer arrives, in a new interpreter ---")
        for message in receive(sqs, answers_url):
            run_host(message, env)

        print("\n--- the same answer arrives a second time ---")
        run_host(answer, env)

        print("\n--- and a different answer, after the fact ---")
        late = dict(answer, answer={"action": "reject", "reason": "changed my mind"})
        run_host(late, env)

        calls = calls_recorded(ddb, "order-4471")
        left = receive(sqs, questions_url)
        print(f"\nfurther messages on the questions queue: {len(left)}")
        print(f"refunds recorded in DynamoDB for order-4471, across all four runs: {calls}")
    return 0


def _plain(value):
    """DynamoDB hands numbers back as Decimal; print them as numbers."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def graph_over_limit() -> str:
    import graph as refund

    return refund.over_limit()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        host(json.loads(sys.argv[1]))
    else:
        sys.exit(main())
