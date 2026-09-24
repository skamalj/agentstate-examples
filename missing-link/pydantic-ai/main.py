"""PydanticAI: message history (short-term) -> reducer -> on_prune -> harness MemoryStore (long-term).

Zero-LLM: ``TestModel`` answers, ``KVHistoryStore`` over an in-memory KV persists the
history, ``append_memory`` does a CAS-safe append into a topic file the harness can
``read_memory`` / ``search_memory``. Swap ``InMemoryKVMemoryStore`` for
``pydantic_ai_dynamodb_memory.DynamoDBMemoryStore`` (or Cosmos / Firestore / Postgres).
"""
import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.test import TestModel
from pydantic_ai_memory_core import append_memory
from pydantic_ai_memory_core.testing import InMemoryKVMemoryStore
from pydantic_ai_persistence.history import KVHistoryStore
from pydantic_ai_persistence.kv import InMemoryAsyncKV

from agentstate_reducer import Background, MessageReducer, ReducerConfig

TURNS = ["I moved to Hanoi last month", "I prefer late flights", "weather?", "coffee?", "book friday", "thanks"]


def text_of(msg) -> str:
    who = "user" if isinstance(msg, ModelRequest) else "assistant"
    parts = [getattr(p, "content", "") for p in msg.parts if isinstance(getattr(p, "content", None), str)]
    return f"{who}: {' '.join(parts)}"


def with_roles(messages):
    """The reducer prunes by role (human / ai). PydanticAI messages carry ``kind`` (request /
    response) instead, so present each one as a dict with a role and keep the original in ``raw``."""
    return [{"role": "human" if isinstance(m, ModelRequest) else "ai", "content": text_of(m), "raw": m}
            for m in messages]


def build():
    memory = InMemoryKVMemoryStore()
    history = KVHistoryStore(InMemoryAsyncKV())

    def remember(pruned, namespace):                          # the reducer's on_prune hook
        text = "\n".join(f"- {m['content']}" for m in pruned)
        asyncio.run(append_memory(memory, f"{namespace}/main/pruned.md", text))

    remember = Background(remember, workers=1)   # worker thread: no running loop there, so asyncio.run is fine
    reducer = MessageReducer(config=ReducerConfig(min_messages=4, max_messages=6,
                                                  preserve_first=False, on_prune=[remember]))
    agent = Agent(TestModel(custom_output_text="noted"))
    return agent, reducer, history, memory, remember


async def run(user_id: str = "kamal", conversation_id: str = "c1"):
    agent, reducer, history, memory, remember = build()
    for turn in TURNS:
        prior = await history.load(conversation_id) or []
        result = await agent.run(turn, message_history=prior)
        reduced = reducer.reduce(existing=with_roles(result.all_messages()), namespace=user_id)
        await history.save(conversation_id, [m["raw"] for m in reduced.surviving])   # the persistence write

    remember.close()                                          # drain before we look
    window = await history.load(conversation_id)
    page = await memory.read(f"{user_id}/main/pruned.md", max_chars=4000)
    hits = await memory.search(f"{user_id}/", "Hanoi", limit=5, max_files=10, max_chars=4000, max_file_chars=1000)
    return window, page.content if page else "", hits


if __name__ == "__main__":
    window, page, hits = asyncio.run(run())
    print(f"history holds {len(window)} messages (short-term, this conversation):")
    for m in window:
        print(f"  {text_of(m)}")
    print("kamal/main/pruned.md (long-term topic file):")
    print(page)
