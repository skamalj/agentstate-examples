"""Two clocks, and only one of them is real.

`WaitPolicy(timeout=...)` publishes `expires_at`. Nothing enforces it: the consumer looks
at it, decides the deadline passed, and sends the policy's `default` as an ordinary answer.

`DynamoDBSaver(ttl_seconds=...)` writes a `ttl` attribute. AWS enforces it: the parked
thread is deleted, whether or not anyone is still holding a question about it.

This script sets the first to three days and the second to one, parks a thread, and prints
both numbers off the real objects. Then it does by hand what DynamoDB's sweeper will do on
its own -- delete the thread's items -- and delivers the answer anyway.

    uv run python two_clocks.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import uuid

import boto3

from local_aws import LocalAws

sys.stdout.reconfigure(encoding="utf-8")

TABLE = os.environ.get("REFUND_TTL_TABLE", "refund-checkpoints-ttl")
SIDE_EFFECTS = os.environ.get("REFUND_SIDE_EFFECT_TABLE", "refund-side-effects")
CHECKPOINT_TTL_SECONDS = 24 * 60 * 60  # one day
# graph.py's FINANCE policy uses REFUND_WAIT_TIMEOUT, default P3D -- three days.


# --------------------------------------------------------------------------- the child
def child(action: str, thread_id: str) -> None:
    from langgraph.types import Command
    from langgraph_dynamodb_checkpoint import DynamoDBSaver

    from agent_wait import InMemoryAnnounce
    from agent_wait.langgraph import publish_interrupts

    import graph as refund

    saver = DynamoDBSaver(TABLE, ttl_seconds=CHECKPOINT_TTL_SECONDS)
    agent = refund.build_agent(saver)
    config = {"configurable": {"thread_id": thread_id}}

    if action == "park":
        result = agent.invoke({"messages": [("user", refund.over_limit())]}, config)
        seen = InMemoryAnnounce()
        publish_interrupts(result, thread_id, [seen])
        envelope = seen.last()
        print("QUESTION_ID=" + envelope.question_id)
        print("EXPIRES_AT=" + str(envelope.expires_at))
    else:
        question_id = os.environ["REFUND_QUESTION_ID"]
        result = agent.invoke(Command(resume={question_id: {"action": "approve"}}), config)
        print(f"  [pid {os.getpid()}] refunds issued this process: {refund.REFUNDS_ISSUED}")
        print(f"  [pid {os.getpid()}] agent says: {result['messages'][-1].text.strip()[:120]}")


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


def field(out: str, name: str) -> str:
    return next(line.split("=", 1)[1].strip() for line in out.splitlines() if line.startswith(name + "="))


def main() -> int:
    thread_id = f"order-4471-{uuid.uuid4().hex[:8]}"
    with LocalAws() as local:
        ddb = boto3.resource("dynamodb")
        import act3_asks

        act3_asks.make_tables(ddb)
        env = dict(local.child_env, REFUND_SIDE_EFFECT_TABLE=SIDE_EFFECTS)

        print(f"thread_id = {thread_id}")
        print(f"WaitPolicy(timeout={os.environ.get('REFUND_WAIT_TIMEOUT', 'P3D')!r})")
        print(f"DynamoDBSaver(ttl_seconds={CHECKPOINT_TTL_SECONDS})")

        out = run_child("park", thread_id, env)
        question_id, expires_at = field(out, "QUESTION_ID"), field(out, "EXPIRES_AT")

        table = ddb.Table(TABLE)
        items = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(thread_id),
            ConsistentRead=True,
        )["Items"]
        ttl = min(int(i["ttl"]) for i in items)
        ttl_iso = dt.datetime.fromtimestamp(ttl, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        spec = ddb.meta.client.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]

        print(f"\nparked items: {len(items)}, every one with a ttl attribute")
        print(json.dumps(
            {
                "table TTL": spec,
                "checkpoint ttl (epoch)": ttl,
                "checkpoint ttl (utc)": ttl_iso,
                "envelope expires_at": expires_at,
            },
            indent=2,
        ))
        gap = dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        ) - dt.datetime.fromtimestamp(ttl, dt.timezone.utc)
        print(f"\nthe question outlives the thread by {gap}")

        print("\n--- DynamoDB reaches the ttl and deletes the thread ---")
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        left = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(thread_id),
            ConsistentRead=True,
        )["Items"]
        print(f"items for this thread: {len(left)}")

        print("\n--- day two: finance approves the question it can still see ---")
        run_child("resume", thread_id, dict(env, REFUND_QUESTION_ID=question_id))
        print(f"\nrefunds recorded in DynamoDB for order-4471: {act3_asks.calls_recorded(ddb, 'order-4471')}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1], sys.argv[2])
    else:
        sys.exit(main())
