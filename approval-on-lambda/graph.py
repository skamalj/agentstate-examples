"""The refund agent. Nothing in here knows about DynamoDB, SQS or Lambda.

A model, two tools, and a limit. `issue_refund` is an ordinary tool: the model calls it,
the money moves, the run ends. `escalate_refund` is the same body behind `@wait(FINANCE)`:
calling it parks the graph on `{"function": "escalate_refund", "args": {...}}` instead of
running it, and the run ends with `__interrupt__` in the result.

Two tools rather than one `if`, because `@wait` is not conditional -- it parks every call
to the function it decorates. The routing is the model's, off the tool descriptions and
the limit in the system prompt. The same module runs in a notebook, in a test, and inside
a Lambda behind a queue; what changes is the checkpointer passed to `build_agent()` and
what the host does with the interrupt afterwards.

`REFUNDS_ISSUED` is the side effect that must never happen twice. Every test ends by
counting it.
"""

from __future__ import annotations

import os

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool

from agent_wait import WaitPolicy
from agent_wait.langgraph import wait

CHAT_MODEL = os.environ.get("BEDROCK_CHAT_MODEL", "global.anthropic.claude-sonnet-4-6")

APPROVAL_LIMIT = 25_000
"""Rupees. Above this, finance decides."""

REFUNDS_ISSUED: list[str] = []
"""The irreversible call, in a list. Cleared by `reset()`."""

# Three days is the real policy. The two-clocks section and the deployed run override it
# through the environment so a deadline can actually be watched to pass.
TIMEOUT = os.environ.get("REFUND_WAIT_TIMEOUT", "P3D")

FINANCE = WaitPolicy(
    timeout=TIMEOUT,
    # Every field here is published and none of it is enforced. `timeout` becomes an
    # absolute `expires_at` on the envelope and `default` becomes the answer to send when
    # it lapses -- but agent-wait never sees an answer, so whoever consumes the envelope
    # is the one that decides the deadline passed.
    default={"action": "reject", "reason": f"no finance response within {TIMEOUT}"},
    allowed_actions=("approve", "reject"),
    tags={"approver_group": "finance"},
)

SYSTEM = (
    "You are a refunds agent for an online store. When the customer asks for a refund, "
    "call exactly one tool, once, with the order id and the amount in rupees: "
    "issue_refund for {limit} rupees or less, escalate_refund for more than {limit}. "
    "Then tell the customer, in one sentence, what the tool returned. Do not invent an order id."
).format(limit=APPROVAL_LIMIT)


@tool
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order of 25000 rupees or less. Runs immediately."""
    # The limit is enforced here, not in the prompt. The model choosing the wrong tool is
    # a routing mistake; the money moving without finance would be an incident. The model
    # gets the refusal back as the tool result and can call escalate_refund instead.
    if amount > APPROVAL_LIMIT:
        return f"refused: {amount} is over the {APPROVAL_LIMIT} approval limit; call escalate_refund"
    return _refund(order_id, amount)


@tool
@wait(FINANCE)
def escalate_refund(order_id: str, amount: int) -> str:
    """Refund an order of more than 25000 rupees. Finance approves before any money moves."""
    return "finance approved; " + _refund(order_id, amount)


def _refund(order_id: str, amount: int) -> str:
    REFUNDS_ISSUED.append(order_id)  # <- the irreversible call
    _record(order_id)
    return f"refunded {amount} for {order_id}"


def _record(order_id: str) -> None:
    """Mirror the side effect into DynamoDB when deployed.

    An in-memory list proves nothing about a Lambda you are not inside. The update is a
    deliberate unconditional `ADD calls :one`: if the tool ever ran twice the counter
    says 2, which is the one failure this whole design exists to prevent.
    """
    table_name = os.environ.get("REFUND_SIDE_EFFECT_TABLE")
    if not table_name:
        return
    import boto3

    boto3.resource("dynamodb").Table(table_name).update_item(
        Key={"pk": f"REFUND#{order_id}", "sk": "#"},
        UpdateExpression="ADD #calls :one",
        ExpressionAttributeNames={"#calls": "calls"},
        ExpressionAttributeValues={":one": 1},
    )


def reset() -> None:
    REFUNDS_ISSUED.clear()


def build_agent(checkpointer):
    """The whole agent. `checkpointer` is the only thing that decides what survives."""
    model = ChatBedrockConverse(model=CHAT_MODEL, temperature=0, max_tokens=400)
    return create_agent(
        model,
        tools=[issue_refund, escalate_refund],
        system_prompt=SYSTEM,
        checkpointer=checkpointer,
    )


def under_limit(order_id: str = "order-3120", amount: int = 1_800) -> str:
    return f"Please refund order {order_id}, {amount} rupees, the parcel arrived damaged."


def over_limit(order_id: str = "order-4471", amount: int = 41_000) -> str:
    return f"Please refund order {order_id}, {amount} rupees, the laptop was never delivered."
