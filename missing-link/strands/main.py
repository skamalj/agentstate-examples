"""Strands: SQLSessionManager (short-term) + reducer + on_prune -> MemoryManager (long-term).

Strands session managers do NOT run the reducer, so this example calls ``reduce()``
itself after each turn. Zero-LLM: a scripted ``Model`` and a dict-backed ``MemoryStore``;
swap the store for ``strands_postgres_store.PostgresMemoryStore`` (or DynamoDB / MongoDB).
"""
import asyncio
import tempfile
import uuid
from typing import Any

from strands import Agent
from strands.memory import MemoryEntry, MemoryManager
from strands.models import Model
from strands_session_sql import SQLSessionManager

from agentstate_reducer import MessageReducer, ReducerConfig

TURNS = ["I moved to Hanoi last month", "I prefer late flights", "weather?", "coffee?", "book friday", "thanks"]


class ScriptedModel(Model):
    """Echoes the last user message. Stands in for Bedrock / OpenAI so the run is offline."""

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *a, **kw):
        raise NotImplementedError

    async def stream(self, messages, *a, **kw):
        last = messages[-1]["content"][0].get("text", "")
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": f"noted: {last}"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                            "metrics": {"latencyMs": 0}}}


class DictMemoryStore:
    """Smallest possible Strands ``MemoryStore``: substring search over a list."""

    name = "memories"
    description = "user facts"
    max_search_results = 5
    writable = True
    extraction = None

    def __init__(self):
        self.rows: list[dict[str, Any]] = []

    async def add(self, content, metadata=None):
        self.rows.append({"content": content, "metadata": metadata or {}})

    async def search(self, query, options=None):
        words = query.lower().split()
        return [MemoryEntry(content=r["content"], metadata=r["metadata"])
                for r in self.rows if any(w in r["content"].lower() for w in words)]


def text_of(msg) -> str:
    return " ".join(b.get("text", "") for b in msg["content"] if isinstance(b, dict))


def build(db_url: str, session_id: str):
    store = DictMemoryStore()
    manager = MemoryManager(stores=[store], injection=False, search_tool_config=False)

    def remember(pruned, namespace):                          # the reducer's on_prune hook
        for m in pruned:                                      # raw turns; call ModelExtractor here if wanted
            asyncio.run(manager.add(text_of(m), {"metadata": {"user": namespace, "role": m["role"]}}))

    reducer = MessageReducer(config=ReducerConfig(min_messages=4, max_messages=6,
                                                  preserve_first=False, on_prune=[remember]))
    agent = Agent(model=ScriptedModel(), agent_id="assistant", callback_handler=None,
                  session_manager=SQLSessionManager(session_id=session_id, url=db_url))
    return agent, reducer, manager, store


def run(user_id: str = "kamal"):
    tmp = tempfile.mkdtemp()
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    agent, reducer, manager, store = build(f"sqlite:///{tmp}/sessions.db", session_id)
    for turn in TURNS:
        agent(turn)
        result = reducer.reduce(existing=agent.messages, namespace=user_id)   # sessions don't do this for you
        agent.messages[:] = result.surviving                                   # in-process window only (see post)

    memories = [e.content for e in asyncio.run(manager.search("Hanoi flights"))]
    return agent.messages, memories, store


if __name__ == "__main__":
    window, memories, _ = run()
    print(f"agent.messages holds {len(window)} messages (short-term, this session):")
    for m in window:
        print(f"  {m['role']:>9}: {text_of(m)}")
    print("long-term memories (manager.search):")
    for c in memories:
        print(f"  - {c}")
