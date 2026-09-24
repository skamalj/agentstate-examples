"""LangChain ``create_agent`` (langchain>=1.0) with the same ReducingSaver + langgraph-memory hook.

``create_agent`` compiles to a LangGraph graph, so the checkpointer is the same
``BaseCheckpointSaver`` slot and the reducer works unchanged. Zero-LLM:
``GenericFakeChatModel`` replays scripted replies; extractor and embedder are the
rule-based ones from ``main.py``.
"""
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph_store_core.testing import FakeEmbeddings

from agentstate_reducer import Background, MessageReducer, ReducerConfig
from agentstate_reducer.langgraph import ReducingSaver
from langgraph_memory import MemoryConfig, MemoryEngine
from main import DIMS, TURNS, rule_consolidator, rule_extractor


def build():
    store = InMemoryStore(index={"dims": DIMS, "embed": FakeEmbeddings(DIMS), "fields": ["content"]})
    engine = MemoryEngine(store, extractor=rule_extractor, consolidator=rule_consolidator,
                          config=MemoryConfig(consolidation_threshold=1.0))

    remember = Background(engine.on_prune)
    reducer = MessageReducer(config=ReducerConfig(min_messages=4, max_messages=6,
                                                  preserve_first=False, on_prune=[remember]))

    replies = (AIMessage(content="noted") for _ in iter(int, 1))                # a fresh message (new id) per turn
    model = GenericFakeChatModel(messages=replies)                              # stands in for any chat model
    agent = create_agent(model, tools=[],
                         checkpointer=ReducingSaver(InMemorySaver(), reducer),  # same slot, same reducer
                         store=store)
    return agent, engine, remember


def run(user_id: str = "kamal"):
    agent, engine, remember = build()
    namespace = ("memories", user_id)
    cfg = {"configurable": {"thread_id": uuid4().hex, "memory_namespace": namespace}}
    for turn in TURNS:
        agent.invoke({"messages": [("user", turn)]}, config=cfg)
    remember.close()

    window = agent.get_state(cfg).values["messages"]
    memories = [r.record.content for r in engine.recall("moving to a new city", namespace, limit=5)]
    return window, memories


if __name__ == "__main__":
    window, memories = run()
    print(f"checkpoint holds {len(window)} messages (short-term, this thread):")
    for m in window:
        print(f"  {m.type:>5}: {m.content}")
    print("long-term memories for the user (recalled by similarity):")
    for c in memories:
        print(f"  - {c}")
