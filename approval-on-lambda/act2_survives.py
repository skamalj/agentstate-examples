"""Act 2. The parked thread survives the process.

Same agent, same thread id, one line different: `DynamoDBSaver` instead of `InMemorySaver`.

This script is the parent. It runs the graph in *child interpreters* -- one to park, one
to resume -- so "the process died" is a real process exit, not a comment. Between the two
it prints the rows that exist in DynamoDB at that moment.

    uv run python act2_survives.py            # moto_server, nothing in your account
    AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8000 uv run python act2_survives.py
    AWS_ENDPOINT_URL_DYNAMODB= uv run python act2_survives.py   # real DynamoDB
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import boto3

from local_aws import LocalAws

sys.stdout.reconfigure(encoding="utf-8")  # the model writes rupee signs

TABLE = os.environ.get("REFUND_CHECKPOINT_TABLE", "refund-checkpoints")


# --------------------------------------------------------------------------- the child
def child(action: str, thread_id: str) -> None:
    """One graph run, one interpreter, then exit. Called as `python act2_survives.py <action>`."""
    from langgraph.types import Command
    from langgraph_dynamodb_checkpoint import DynamoDBSaver

    import graph as refund

    saver = DynamoDBSaver(TABLE)  # creates the table if it is not there
    agent = refund.build_agent(saver)
    config = {"configurable": {"thread_id": thread_id}}

    if action == "park":
        result = agent.invoke({"messages": [("user", refund.over_limit())]}, config)
        interrupts = result.get("__interrupt__") or []
        print(f"  [pid {os.getpid()}] parked on {len(interrupts)} question(s)")
        for item in interrupts:
            print(f"  [pid {os.getpid()}] question_id = {item.id}")
            print(f"  [pid {os.getpid()}] question    = {item.value['question']}")
        print(f"  [pid {os.getpid()}] refunds issued this process: {refund.REFUNDS_ISSUED}")
        # Hand the id to the parent through stdout. In the deployed version it travels on
        # the envelope instead.
        print("QUESTION_ID=" + interrupts[0].id)
    elif action == "resume":
        question_id = os.environ["REFUND_QUESTION_ID"]
        answer = {"action": "approve", "by": "finance", "note": "vendor confirmed non-delivery"}
        result = agent.invoke(Command(resume={question_id: answer}), config)
        print(f"  [pid {os.getpid()}] refunds issued this process: {refund.REFUNDS_ISSUED}")
        print(f"  [pid {os.getpid()}] agent says: {result['messages'][-1].text.strip()}")
    else:
        raise SystemExit(f"unknown action {action}")


# -------------------------------------------------------------------------- the parent
def run_child(action: str, thread_id: str, env: dict[str, str]) -> str:
    out = subprocess.run(
        [sys.executable, __file__, action, thread_id],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sys.stdout.write(out.stdout)
    if out.returncode:
        sys.stdout.write(out.stderr)
        raise SystemExit(f"child {action!r} exited {out.returncode}")
    return out.stdout


def items_for(thread_id: str) -> list[dict]:
    table = boto3.resource("dynamodb").Table(TABLE)
    return table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(thread_id),
        ConsistentRead=True,
    )["Items"]


def describe(items: list[dict]) -> None:
    print(f"\nQuery on PK={items[0]['PK']!r}: {len(items)} items")
    for item in sorted(items, key=lambda i: i["checkpoint_key"]):
        kind = item["checkpoint_key"].split("$", 1)[0]
        size = len(item.get("checkpoint") or item.get("value") or b"")
        extra = f"channel={item['channel']!r}" if kind == "writes" else f"type={item['type']!r}"
        print(f"  {kind:10} SK={item['SK']!r}")
        print(f"             {extra}, {size} bytes, ttl={item.get('ttl', '-')}")


def main() -> int:
    thread_id = os.environ.get("REFUND_THREAD_ID") or f"order-4471-{uuid.uuid4().hex[:8]}"
    with LocalAws() as local:
        env = local.child_env
        print(f"thread_id = {thread_id}   table = {TABLE}   parent pid = {os.getpid()}")

        print("\n--- interpreter 1: the customer asks ---")
        out = run_child("park", thread_id, env)
        question_id = next(
            line.split("=", 1)[1].strip() for line in out.splitlines() if line.startswith("QUESTION_ID=")
        )

        print("\n--- interpreter 1 is gone. What is in DynamoDB right now ---")
        items = items_for(thread_id)
        describe(items)

        print("\n--- interpreter 2: finance answers, three days later ---")
        env = dict(env, REFUND_QUESTION_ID=question_id)
        run_child("resume", thread_id, env)

        after = items_for(thread_id)
        print(f"\nitems after the resume: {len(after)} (was {len(items)})")
        print(json.dumps({"thread_id": thread_id, "question_id": question_id}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1], sys.argv[2])
    else:
        sys.exit(main())
