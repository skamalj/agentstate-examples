"""Act 1. The question vanishes with the process.

The graph parks on a refund that needs finance. `InMemorySaver` holds the parked thread
in this interpreter's heap. Then the interpreter exits -- which on Lambda happens within
minutes of the last invocation, and in a container happens on the next deploy.

Run it twice. The second run starts a fresh interpreter with the same thread id and finds
nothing to resume.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import graph as refund

THREAD = "order-4471"


def main() -> int:
    refund.reset()
    agent = refund.build_agent(InMemorySaver())
    config = {"configurable": {"thread_id": THREAD}}

    result = agent.invoke({"messages": [("user", refund.over_limit())]}, config)
    interrupts = result.get("__interrupt__") or []
    print(f"run 1: parked on {len(interrupts)} question(s)")
    for item in interrupts:
        print(f"  question_id={item.id}")
        print(f"  value={item.value}")
    print(f"run 1: refunds issued = {refund.REFUNDS_ISSUED}")

    # Finance answers, in a *new* interpreter, with a new InMemorySaver -- exactly what
    # the next Lambda invocation would be.
    print("\n--- interpreter exits here ---\n")
    fresh = refund.build_agent(InMemorySaver())
    answer = Command(resume={interrupts[0].id: {"action": "approve"}})
    try:
        after = fresh.invoke(answer, config)
        print(f"run 2: messages on the thread: {len(after['messages'])}")
        print(f"run 2: agent says: {after['messages'][-1].text.strip()}")
    except Exception as exc:
        print(f"run 2: {type(exc).__name__}: {exc}")
    print(f"run 2: refunds issued = {refund.REFUNDS_ISSUED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
