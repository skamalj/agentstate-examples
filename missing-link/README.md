# missing-link

Runnable code behind the post **"Every agent framework has a checkpointer and a store. None of them connects the two."** (`blog.md` in this folder).

One uv project per framework, because the four frameworks do not share a dependency set. Every example runs offline: scripted models, deterministic embedders, in-memory or SQLite stores. Each `main.py` prints the short-term window and the long-term memory side by side; each test asserts that the turns which left the window reached the memory side, once each.

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
