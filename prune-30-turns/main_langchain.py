"""The same 30 turns through LangChain's create_agent instead of a hand-built LangGraph graph.

Nothing about the reducer changes: create_agent takes a checkpointer, and ReducingSaver is one.
The memory engine, the on_prune hook and the window are imported from main.py. The fresh thread
uses engine.tools() (search_memory) instead of a recall node, so the agent looks memories up itself.
"""
from __future__ import annotations

import sys
import time
from uuid import uuid4

from langchain.agents import create_agent
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agentstate_reducer.langgraph import ReducingSaver
from main import (CHAT_MODEL, EMBED_MODEL, MAX_MESSAGES, MIN_MESSAGES, NAMESPACE, SYSTEM, TURNS,
                  build_engine, build_reducer, log, log_turn, short)


def run() -> dict:
    sys.stdout.reconfigure(encoding="utf-8")
    llm = ChatBedrockConverse(model=CHAT_MODEL, temperature=0, max_tokens=300)
    embed = BedrockEmbeddings(model_id=EMBED_MODEL)
    engine, retries = build_engine(llm, embed)
    reducer, prune_events = build_reducer(engine)
    saver = ReducingSaver(InMemorySaver(), reducer)

    # ---- phase 1: LangChain agent, no tools, bounded checkpoint
    agent = create_agent(llm, tools=[], system_prompt=SYSTEM, checkpointer=saver)

    thread_a = f"lc-trip-{uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread_a, "memory_namespace": NAMESPACE}}
    log(f"framework       : langchain.agents.create_agent")
    log(f"chat model      : {CHAT_MODEL}")
    log(f"window          : max_messages={MAX_MESSAGES} min_messages={MIN_MESSAGES}")
    log(f"thread A        : {thread_a}   namespace={NAMESPACE}")
    log()

    t0 = time.time()
    total_pruned = 0
    for i, text in enumerate(TURNS, 1):
        before = len(prune_events)
        out = agent.invoke({"messages": [HumanMessage(content=text, id=f"h{i:02d}")]}, config=cfg)
        window = saver.get_tuple(cfg).checkpoint["channel_values"]["messages"]
        total_pruned += log_turn(i, text, out["messages"][-1].content, len(window), prune_events[before:])
    elapsed = time.time() - t0

    final = saver.get_tuple(cfg).checkpoint["channel_values"]["messages"]
    memories = engine.list(NAMESPACE)
    log()
    log("=== after 30 turns (create_agent) ===")
    log(f"messages produced        : {2 * len(TURNS)}")
    log(f"messages in checkpoint   : {len(final)}")
    log(f"messages delivered to on_prune (exactly once each): {total_pruned}")
    log(f"prune events             : {len(prune_events)}")
    log(f"memories in {NAMESPACE}: {len(memories)}")
    log(f"wall time for 30 turns   : {elapsed:.0f}s")
    log(f"empty extractions retried: {retries['count']}")

    # ---- phase 2: fresh thread, the agent gets engine.tools() and looks memories up itself
    log()
    log("=== fresh thread, same memory_namespace, agent has engine.tools() ===")
    saver_b = ReducingSaver(InMemorySaver(), reducer)
    agent_b = create_agent(
        llm, tools=engine.tools(),
        system_prompt=SYSTEM + " Before answering anything about the user, call search_memory.",
        checkpointer=saver_b,
    )
    thread_b = f"lc-followup-{uuid4().hex[:8]}"
    cfg2 = {"configurable": {"thread_id": thread_b, "memory_namespace": NAMESPACE}}
    log(f"thread B: {thread_b}")

    tool_outputs: list[str] = []
    answers: list[str] = []
    questions = [
        "I'm back. Remind me: what flight timing did I settle on, and who is coming with me?",
        "Also, when does my passport expire, and which seat should you book for me?",
    ]
    for q in questions:
        log()
        log(f"user : {q}")
        n_before = len(saver_b.get_tuple(cfg2).checkpoint["channel_values"]["messages"]) if saver_b.get_tuple(cfg2) else 0
        out = agent_b.invoke({"messages": [HumanMessage(content=q, id=str(uuid4()))]}, config=cfg2)
        for m in out["messages"][n_before:]:
            if m.type == "ai" and getattr(m, "tool_calls", None):
                for tc in m.tool_calls:
                    log(f"tool call: {tc['name']}({tc['args']})")
            elif m.type == "tool":
                tool_outputs.append(str(m.content))
                for line in str(m.content).splitlines():
                    log(f"  {short(line, 150)}")
        answers.append(out["messages"][-1].content)
        log(f"agent: {out['messages'][-1].content}")

    return {
        "final_window": final, "memories": memories, "prune_events": prune_events,
        "total_pruned": total_pruned, "tool_outputs": tool_outputs, "answers": answers,
    }


if __name__ == "__main__":
    run()
