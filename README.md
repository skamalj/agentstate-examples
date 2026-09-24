# agentstate examples

Runnable code behind the blog posts and docs for the agentstate packages: `agentstate-reducer`, the LangGraph / CrewAI / Strands / PydanticAI checkpointers, stores and memory backends.

Each folder is a self-contained [uv](https://docs.astral.sh/uv/) project. Every snippet in a post is taken from its folder and runs there:

```bash
cd <folder>
uv sync
uv run pytest -q          # the tests that back the post's claims
uv run python main.py     # the demo itself
```

| Folder | Post |
|---|---|
| `prune-30-turns/` | We ran the pruner for 30 turns. Here is what the agent remembered. |
| `missing-link/` | Every agent framework has a checkpointer and a store. None of them connects the two. |
| `approval-on-lambda/` | Human-in-the-loop LangGraph on AWS Lambda. |

Docs: <https://skamalj.github.io/agentstate-reducer/>

MIT
