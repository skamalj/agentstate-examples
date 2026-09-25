"""Act 4. The same host, on Lambda, behind a queue.

One message, one graph run, one publish. The `if` that tells an answer from a start is
the documented rule written out, and it is the only thing in this file that is not
boilerplate.

The graph is built at import time so the checkpointer and the model client are reused
across warm invocations. Nothing else is cached: a cold start reloads the thread from
DynamoDB, which is the whole point.

Environment:
    REFUND_CHECKPOINT_TABLE   the checkpointer's table (pre-created by the stack)
    REFUND_APPROVALS_TABLE    where DynamoDbAnnounce writes the open question
    REFUND_QUESTIONS_QUEUE    the FIFO queue questions go out on, for whoever answers
    REFUND_TIMEOUTS_QUEUE     the same envelope, for the scheduler that enforces the deadline
    REFUND_SIDE_EFFECT_TABLE  the counter graph.py bumps when the refund actually runs
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from langgraph.types import Command
from langgraph_dynamodb_checkpoint import DynamoDBSaver

from agent_wait import LogAnnounce
from agent_wait.aws import DynamoDbAnnounce, SqsAnnounce
from agent_wait.langgraph import publish_interrupts

import graph as refund

# The Lambda runtime leaves the root logger at WARNING, so a library logging at INFO is
# silent in production -- which is exactly when you want to know what was published.
logging.getLogger("agent_wait").setLevel(os.environ.get("AGENT_WAIT_LOG_LEVEL", "INFO").upper())

_log = logging.getLogger("refund_agent")
_log.setLevel(logging.INFO)

_ttl = os.environ.get("REFUND_CHECKPOINT_TTL_SECONDS")
AGENT = refund.build_agent(
    DynamoDBSaver(os.environ["REFUND_CHECKPOINT_TABLE"], ttl_seconds=int(_ttl) if _ttl else None)
)

# One call, four destinations, and they are not variations on each other: a log line, the
# queue a person reads, the queue a *scheduler* reads, and a row an operator can query for
# "what is open right now". The library writes to all four and reads from none of them.
ANNOUNCE = [
    LogAnnounce(),
    SqsAnnounce(os.environ["REFUND_QUESTIONS_QUEUE"]),
    SqsAnnounce(os.environ["REFUND_TIMEOUTS_QUEUE"]),
    DynamoDbAnnounce(os.environ["REFUND_APPROVALS_TABLE"]),
]


def route(message: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = str(message["thread_id"])
    config = {"configurable": {"thread_id": thread_id}}

    if "question_id" in message:  # an answer, in the documented shape
        value: Any = Command(resume={str(message["question_id"]): message.get("answer")})
    else:  # a start
        value = message.get("input")

    result = AGENT.invoke(value, config)
    publish_interrupts(result, thread_id, ANNOUNCE)

    parked = [i.id for i in (result.get("__interrupt__") or [])]
    _log.info(
        json.dumps(
            {
                "thread_id": thread_id,
                "kind": "answer" if "question_id" in message else "start",
                "parked_on": parked,
                # Module state, so this is everything this *container* has refunded --
                # a warm invocation that ran nothing still reports the earlier ones. The
                # count that means anything is the one in DynamoDB.
                "refunds_by_this_container": list(refund.REFUNDS_ISSUED),
                "reply": "" if parked else result["messages"][-1].text.strip(),
            }
        )
    )
    return {"thread_id": thread_id, "parked_on": parked}


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """SQS entry point. One record at a time, keyed by thread."""
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            route(json.loads(record["body"]))
        except Exception:
            _log.exception("failed on message %s", record.get("messageId"))
            failures.append({"itemIdentifier": str(record.get("messageId", ""))})
    return {"batchItemFailures": failures}
