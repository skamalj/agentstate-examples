# prune-30-turns

Code for the post *We ran the pruner for 30 turns. Here is what the agent remembered* (`blog.md` here).

One user, one 30-turn travel-planning conversation, a bounded LangGraph checkpoint
(`max_messages=10`, `min_messages=6`) and long-term memory built from the turns that
leave the window. A fresh thread then recalls facts that only ever existed in pruned turns.

| piece | package |
|---|---|
| bounded checkpoint on any saver | `agentstate-reducer[langgraph]` 0.5.0 (`ReducingSaver`, `MessageReducer`) |
| extract / consolidate / recall | `langgraph-memory` 0.1.0 (`MemoryEngine`) |
| checkpointer and store | LangGraph `InMemorySaver`, `InMemoryStore(index=...)` |
| models | AWS Bedrock via `langchain-aws` (Claude Sonnet 4.6 + Titan Embed v2, 1024 dims) |

Docs: https://skamalj.github.io/agentstate-reducer/

## Run

```bash
uv venv -p 3.12
uv sync
export AWS_PROFILE=<your sso profile> AWS_DEFAULT_REGION=ap-south-1
# optional: export BEDROCK_CHAT_MODEL=<bedrock model id or inference profile>
uv run python main.py | tee run.log                      # hand-built LangGraph graph
uv run python main_langchain.py | tee run-langchain.log  # same turns via langchain.agents.create_agent
uv run pytest -q
```

Dependencies were added with `uv add "agentstate-reducer[langgraph]==0.5.0" "langgraph-memory==0.1.0" langchain-aws boto3`
and `uv add --dev pytest`. `run.log` and `run-langchain.log` are the runs quoted in the post. The tests run
each demo once (about two and a half minutes each) and assert the window bound, the exactly-once delivery
count, the consolidation of the corrected preference, the survival of the user's window-seat memory, and the
recall in the fresh thread. Every invocation is a separate run against a live model.

`main_langchain.py` imports the engine, hook, window and turns from `main.py`, passes
`checkpointer=ReducingSaver(InMemorySaver(), reducer)` to `create_agent`, and in the fresh thread gives the agent
`engine.tools()` so it calls `search_memory` itself instead of using a recall node.

## What the script does

1. Builds `MemoryEngine(InMemoryStore(index=...), chat_model, extractor=..., consolidator=..., config=MemoryConfig(consolidation_threshold=0.4))`.
2. Wraps `InMemorySaver` in `ReducingSaver(inner, MessageReducer(config=ReducerConfig(max_messages=10, min_messages=6, on_prune=[hook])))`.
   The hook is `engine.remember(pruned, namespace)` plus logging (that is what `engine.on_prune` does).
3. Runs 30 scripted user turns on thread A with `memory_namespace=("memories", "asha")`. Turn 17 corrects the
   flight-timing preference stated in turn 4.
4. After each turn prints the checkpoint size, what left the window, and what the engine stored (NEW vs UPDATED).
5. Starts thread B (new saver, empty checkpoint, same namespace), recalls with `engine.recall(..., limit=8)`, and answers
   two questions whose answers only exist in pruned turns.

The hook runs synchronously here so the log lines up with the turns. In production wrap it:
`Background(engine.on_prune)` runs it off the request path.

## Settings that are not the defaults, and why

- `consolidation_threshold=0.4`. With the engine default (0.85) the corrected preference was inserted next to
  the old one instead of replacing it (`run-defaults.log`, 53 memories). Titan Embed v2 cosine between the two
  phrasings of the flight preference measured 0.46; between paraphrases of "planning a trip to Japan" 0.72;
  between unrelated facts 0.06 to 0.23. Probe:

  ```python
  from langchain_aws import BedrockEmbeddings
  import numpy as np
  e = BedrockEmbeddings(model_id="amazon.titan-embed-text-v2:0")
  v = np.array(e.embed_documents([a, b])); v /= np.linalg.norm(v, axis=1, keepdims=True)
  print(v[0] @ v[1])
  ```

- `EXTRACT_PROMPT` names the user. With the default prompt, a pruned chunk that opened with
  "Meera gets motion sick on buses" came out as "Meera prefers overnight flight departures", attributing the
  user's correction to her sister (`run-defaults.log`).

- `CONSOLIDATE_PROMPT` requires the same person and the same attribute for an update. Without it, one run merged
  "The user always books window seats" into "Meera takes the aisle seat" and the fresh thread answered "aisle seat".

- `RECALL_LIMIT = 8`. With 5, the compound first question missed the Meera memory.
