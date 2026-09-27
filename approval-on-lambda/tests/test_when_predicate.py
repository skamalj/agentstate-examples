"""The threshold, tested without a graph, a model or an AWS account.

`@wait(FINANCE, when=...)` decides per call. Under the limit the wrapper never reaches
`interrupt()`, so the tool runs outside any LangGraph runtime and this whole file needs
nothing but the library.

It also pins the library version. The post's claim is that its output came from real runs
against `agent-wait` 0.8; an exact pin in `pyproject.toml` once handed a hand-installed
0.8 straight back to 0.7 on the next `uv run`, which would have produced a run that
demonstrated the old behaviour and looked exactly like the new one. A test is the only
part of that chain that fails loudly.
"""

from __future__ import annotations

import os
import sys

import agent_wait

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import graph as refund  # noqa: E402


def test_the_library_is_new_enough_for_a_conditional_wait():
    major, minor = (int(p) for p in agent_wait.__version__.split(".")[:2])
    assert (major, minor) >= (0, 8), f"when= needs agent-wait 0.8+, got {agent_wait.__version__}"


def test_the_tool_is_conditional_at_all():
    """0.8 records this on the wrapper; on 0.7 the attribute does not exist."""
    assert refund.issue_refund.func.__agent_wait__["conditional"] is True


def test_under_the_limit_it_refunds_and_asks_nobody():
    """No graph, no runtime, no credentials: the predicate says no question is needed."""
    refund.reset()

    out = refund.issue_refund.invoke({"order_id": "order-under", "amount": 1_800})

    assert out == "refunded 1800 for order-under"
    assert refund.REFUNDS_ISSUED == ["order-under"]


def test_over_the_limit_it_does_not_refund():
    """It tries to park instead, which outside a graph runtime cannot succeed.

    The assertion is that the money did not move, not that a particular exception came
    out: what LangGraph raises with no runtime is its business, not a contract this post
    should depend on.
    """
    refund.reset()

    try:
        refund.issue_refund.invoke({"order_id": "order-over", "amount": 41_000})
    except Exception:
        pass

    assert refund.REFUNDS_ISSUED == []


def test_the_threshold_is_the_limit_the_post_quotes():
    """The boundary is inclusive-below: the limit itself does not need finance."""
    refund.reset()
    refund.issue_refund.invoke({"order_id": "order-at", "amount": refund.APPROVAL_LIMIT})
    assert refund.REFUNDS_ISSUED == ["order-at"]

    refund.reset()
    try:
        refund.issue_refund.invoke({"order_id": "order-above", "amount": refund.APPROVAL_LIMIT + 1})
    except Exception:
        pass
    assert refund.REFUNDS_ISSUED == []
