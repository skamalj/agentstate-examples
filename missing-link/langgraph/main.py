"""LangGraph: checkpointer -> reducer (inside the save) -> on_prune -> langgraph-memory -> store.

Zero-LLM: a scripted assistant node, a rule-based extractor, a deterministic embedder.
The only LangGraph-specific line is the ReducingSaver; DynamoDBSaver / CosmosDBSaver /
FirestoreSaver take ``reducer=`` directly.
"""
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph_store_core.testing import FakeEmbeddings

from agentstate_reducer import Background, MessageReducer, ReducerConfig
from agentstate_reducer.langgraph import ReducingSaver
from langgraph_memory import ConsolidationDecision, ExtractedFact, MemoryConfig, MemoryEngine

DIMS = 256
TURNS = [
    "I moved to Hanoi last month",
    "I prefer late flights",
    "what is the weather like",
    "any good coffee nearby",
    "book me something for friday",
    "thanks",
]


def rule_extractor(text: str):
    """Stand-in for the LLM extractor: one fact per line that states a preference or a move."""
    facts = []
    for line in text.splitlines():
        body = line.split(":", 1)[-1].strip()
        if any(k in body.lower() for k in ("moved", "prefer", "likes", "lives")):
            facts.append(ExtractedFact(content=body, categories=["profile"], importance=0.7))
    return facts


def rule_consolidator(fact, similar):
    return ConsolidationDecision(action="insert")


def build():
    store = InMemoryStore(index={"dims": DIMS, "embed": FakeEmbeddings(DIMS), "fields": ["content"]})
    engine = MemoryEngine(store, extractor=rule_extractor, consolidator=rule_consolidator,
                          config=MemoryConfig(consolidation_threshold=1.0))

    remember = Background(engine.on_prune)                     # extraction off the request path
    reducer = MessageReducer(config=ReducerConfig(min_messages=4, max_messages=6,
                                                  preserve_first=False, on_prune=[remember]))
    saver = ReducingSaver(InMemorySaver(), reducer)           # any BaseCheckpointSaver

    def assistant(state: MessagesState):
        return {"messages": [AIMessage(content=f"noted: {state['messages'][-1].content}")]}

    graph = (StateGraph(MessagesState)
             .add_node("assistant", assistant)
             .add_edge(START, "assistant").add_edge("assistant", END)
             .compile(checkpointer=saver, store=store))
    return graph, engine, remember


def run(user_id: str = "kamal"):
    graph, engine, remember = build()
    namespace = ("memories", user_id)
    cfg = {"configurable": {"thread_id": uuid4().hex, "memory_namespace": namespace}}
    for turn in TURNS:
        graph.invoke({"messages": [("user", turn)]}, config=cfg)
    remember.close()                                          # drain the pool before we look

    window = graph.get_state(cfg).values["messages"]
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
