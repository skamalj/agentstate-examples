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
| `prune-30-turns/` | [We ran the pruner for 30 turns. Here is what the agent remembered.](https://medium.com/@skamalj_11034/we-ran-the-pruner-for-30-turns-here-is-what-the-agent-remembered-36b231a81983) |
| `missing-link/` | [Agent State to Long-Term Memory: The Missing Hook](https://medium.com/@skamalj_11034/agent-state-to-long-term-memory-the-missing-hook-f7f659f2434c) — every agent framework has a checkpointer and a store; none of them connects the two. |
| `approval-on-lambda/` | [Externalize agent interrupts for HITL](https://medium.com/@skamalj_11034/externalize-agent-interrupts-for-hitl-cb7566eff6a7) — human-in-the-loop LangGraph on AWS Lambda. |

Docs: <https://skamalj.github.io/agentstate-reducer/>

MIT
