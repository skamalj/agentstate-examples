# missing-link

Runnable code behind the post **"Agent State to Long-Term Memory: The Missing Hook"** (`blog.md` in this folder). Every snippet in the post is cut from a script here, and every script has a test.

Read the post: <https://medium.com/@skamalj_11034/agent-state-to-long-term-memory-the-missing-hook-f7f659f2434c>

One uv project per framework, because the four frameworks do not share a dependency set. Every example runs offline: scripted models, deterministic embedders, in-memory or SQLite stores. No API keys, no databases. Each `main.py` prints the short-term window and the long-term memory side by side; each test asserts that the turns which left the window reached the memory side, once each.

| Folder | Short-term (ours) | Reducer runs where | Long-term (ours / theirs) |
|---|---|---|---|
| `langgraph/` (`main.py` graph, `main_langchain.py` LangChain `create_agent`) | `ReducingSaver(InMemorySaver())` (stand-in for `langgraph-dynamodb-checkpoint`, `-cosmosdb`, `-firestore`, or any saver) | inside the checkpointer's `put` | `langgraph-memory` engine over a `BaseStore` |
| `crewai/` | `crewai-persistence-sql` (SQLite; same code for `-dynamodb`, `-mongodb`, `-cosmosdb`, `-firestore`) | inside `save_state` | CrewAI `Memory` over `crewai-memory-core`'s test backend (swap for `crewai-memory-postgres` etc.) |
| `strands/` | `strands-session-sql` (SQLite) | **you call `reduce()`**; session managers do not | Strands `MemoryManager` over a 20-line `MemoryStore` (swap for `strands-postgres-store` etc.) |
| `pydantic-ai/` | `pydantic-ai-persistence` `KVHistoryStore` | before the history save | harness `MemoryStore` from `pydantic-ai-memory-core` via `append_memory` (swap for `pydantic-ai-dynamodb-memory` etc.) |

## Run

```bash
cd langgraph      # or crewai, strands, pydantic-ai
uv sync
uv run python main.py
uv run pytest -q
```

The `langgraph/` project has a second script, `main_langchain.py`, for LangChain's `create_agent`: `uv run python main_langchain.py`.

Python 3.12 (`uv venv -p 3.12` was used to create each project). Nothing needs an API key or a database.

## What was verified

All tests passed on 2026-09-24 (`uv run pytest -q` in each folder), against these versions from each project's `uv.lock`:

| Example | Test | Key versions |
|---|---|---|
| `langgraph/` (graph, `main.py`) | passed | agentstate-reducer 0.5.0, langgraph 1.2.12, langgraph-memory 0.1.0, langgraph-store-core 0.1.1 |
| `langgraph/` (LangChain `create_agent`, `main_langchain.py`) | passed | langchain 1.4.2, same reducer and engine |
| `crewai/` | passed | agentstate-reducer 0.5.0, crewai 1.15.22, crewai-persistence-sql 0.2.1, crewai-memory-core 0.1.0 |
| `strands/` | passed | agentstate-reducer 0.5.0, strands-agents 1.57.0, strands-session-sql 0.2.0 |
| `pydantic-ai/` | passed | agentstate-reducer 0.5.0, pydantic-ai 2.48.0, pydantic-ai-harness 0.34.0, pydantic-ai-memory-core 0.1.0, pydantic-ai-persistence 0.1.0 |

Each test asserts: the persisted window holds at most six messages, the early turns ("I moved to Hanoi last month", "I prefer late flights") are no longer in it, they are recallable from the long-term side under the user namespace, and each was delivered once.

## How each project was created

```bash
uv init --bare --python 3.12 && uv venv -p 3.12
# langgraph
uv add "agentstate-reducer[langgraph]" langgraph-memory langgraph-store-core langgraph langchain-core "langchain>=1.0" pytest
# crewai
uv add crewai crewai-persistence-sql crewai-memory-core agentstate-reducer pytest
# strands
uv add strands-agents strands-session-sql agentstate-reducer pytest
# pydantic-ai
uv add pydantic-ai-memory-core pydantic-ai-harness pydantic-ai-persistence agentstate-reducer pytest anyio
```

Docs: <https://skamalj.github.io/agentstate-reducer/>
