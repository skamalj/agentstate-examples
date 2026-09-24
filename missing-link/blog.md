# Every agent framework has a checkpointer and a store. None of them connects the two.

LangGraph, CrewAI, Strands and PydanticAI all agree on one thing: an agent needs two kinds of memory. Short-term memory is the conversation you are in. Long-term memory is what you keep about the user after the conversation is gone.

They also agree on the plumbing. Two pipes. One for each.

What none of them ships is the joint between the pipes. When a turn falls out of the context window, nothing moves it into long-term memory. That joint is left for you to build, and the obvious places to build it are all slightly wrong.

This post is about the one place it fits.

## Two pipes, four frameworks

Here is what each framework calls its two pipes. The names differ, the shape does not.

| Framework | Short-term | Long-term |
|---|---|---|
| LangGraph | checkpointer (`BaseCheckpointSaver`, per thread) | store (`BaseStore`, per namespace) |
| CrewAI | Flow state persistence (`@persist`, per flow) | `Memory` (`StorageBackend`, scope paths like `/user/kamal`) |
| Strands | session manager (per session) | `MemoryManager` over `MemoryStore`s |
| PydanticAI | message history you persist yourself, or the harness `StepStore` | harness `MemoryStore` (a notebook of Markdown files) |

I read each framework's current docs before writing this, because "nothing connects them" is the kind of claim that ages badly.

- **LangGraph** describes checkpointers and stores as complementary and separate: a checkpointer tracks the thread, a store tracks durable information across threads. The memory page offers two ways to write long-term memory, "in the hot path" or "in the background", and both are code you add.
- **CrewAI** does write memory automatically, but from *task output*: after each task, facts are extracted from the result and stored. Flow state, the thing `@persist` saves, has no bridge to `Memory`.
- **Strands** extracts memories from live messages through `MemoryManager` on a trigger (every turn, or every N turns), tracking a high-water mark per store. Conversation management, which trims the window, is documented as a separate concern from memory. Nothing hands trimmed messages to a store.
- **PydanticAI's** harness expects the *model* to write memory, through `write_memory`, `read_memory` and `search_memory` tools. There is no link from step history or history compaction to the notebook.

So the claim, precisely: in all four, moving conversation out of the short-term pipe and into the long-term pipe is your code. And your code has to answer three questions the framework does not.

## The three questions

**When?** A graph node runs every turn, so it sees the same messages again and again. A scheduler runs on the clock, which has nothing to do with the conversation. A callback fires on events that are not "this message is about to be forgotten".

**Which?** Whatever trigger you pick, you now need a cursor: which messages have already been extracted? That cursor has to survive restarts, retries and the framework writing the same state twice in one turn.

**What if it fails?** The extraction usually calls an LLM. If it runs on the request path, it adds latency. If it throws, does the user's message get saved or not?

Every ad-hoc bridge I have seen answers these badly, because it is bolted on somewhere that does not know the answer.

## The one place that knows

There is a component that already knows exactly which messages are leaving the window, exactly once, at exactly the moment it matters: the thing that prunes the window before it is saved.

`agentstate-reducer` is that component. It is a zero-dependency Python package that bounds a message list to N messages or N tokens, and the three LangGraph checkpointers, the five CrewAI persistence packages and a wrapper for any other LangGraph saver all run it inside their save. Since 0.4.0 it has a hook:

```python
reducer = MessageReducer(config=ReducerConfig(
    max_messages=20,
    on_prune=[Background(remember)],   # remember(pruned_messages, namespace)
))
```

That is the whole joint. The persistence layer computes `surviving` and `pruned`, writes `surviving`, and hands `pruned` to your hook with a namespace that the *app* chose (the user, the tenant, the account). Nothing else changes.

```
persistence.save(...)
   └─ reducer.reduce(existing=messages, namespace=...)
        ├─ surviving ──► persisted            (short-term: thread / session / flow)
        └─ pruned ─────► on_prune hooks ──► long-term memory (user)
```

What the hook guarantees, and this is the scope of "connects the two":

- **When.** It fires inside the save path, the moment a message stops being visible to the model. Not on a timer, not on every turn.
- **Which.** Each message id reaches the hooks once per reducer instance, even when the framework saves overlapping lists several times per turn. LangGraph writes a checkpoint per super-step; you still get each pruned message once. No cursor to keep.
- **Off the request path.** `Background(fn)` runs the hook on a bounded worker pool. If the pool falls behind it drops a batch with a warning rather than blocking the request. It drains at interpreter exit. On serverless, call `close()` at the end of the handler.
- **Failure is contained.** A hook that raises is logged and skipped. The reduce still returns, the checkpoint is still written, the request still succeeds. I watched this happen while writing the PydanticAI example below: a bug in my hook printed a warning and the history saved anyway.

The reducer never imports a store, a framework or an LLM. The extractor is whatever you put in the hook. Which brings us to the four frameworks.

## LangGraph

The checkpointers `langgraph-dynamodb-checkpoint`, `langgraph-checkpoint-cosmosdb` and `langgraph-checkpoint-firestore` take `reducer=` directly. For anything else, `ReducingSaver` wraps it. The namespace rides in the run config under `memory_namespace`, falling back to the thread.

The extractor here is `langgraph-memory`, whose `engine.on_prune` is already a hook. It extracts facts, consolidates against similar memories and writes to any `BaseStore` built with an index.

```python
engine = MemoryEngine(store, extractor=rule_extractor, consolidator=rule_consolidator)

remember = Background(engine.on_prune)                    # extraction off the request path
reducer = MessageReducer(config=ReducerConfig(max_messages=6, min_messages=4,
                                              preserve_first=False, on_prune=[remember]))
saver = ReducingSaver(InMemorySaver(), reducer)          # any BaseCheckpointSaver
graph = builder.compile(checkpointer=saver, store=store)

cfg = {"configurable": {"thread_id": tid, "memory_namespace": ("memories", "kamal")}}
graph.invoke({"messages": [("user", "I moved to Hanoi last month")]}, config=cfg)
```

After six turns the checkpoint holds four messages and `engine.recall("moving to a new city", ("memories", "kamal"))` returns the Hanoi turn and the flight preference, once each.

**Plain LangChain agents too.** LangChain 1.0's `create_agent` compiles to a LangGraph graph, so its `checkpointer=` is the same slot and the reducer works there unchanged. Same store, same engine, same hook:

```python
agent = create_agent(model, tools=[],
                     checkpointer=ReducingSaver(InMemorySaver(), reducer),   # same slot, same reducer
                     store=store)
agent.invoke({"messages": [("user", "I moved to Hanoi last month")]}, config=cfg)
```

With a fake chat model replying "noted", the result is the same as the graph version: four messages in the checkpoint, the Hanoi turn and the flight preference in the store. One thing the fake model taught me: LangGraph's `add_messages` merges messages that share an id, so a scripted model must return a fresh message object per turn, not the same one on a loop.

One limit: `ReducingSaver` replaces the messages channel value wholesale, so it must not be used on a channel backed by LangGraph's `DeltaChannel`. It also does not change what the model sees mid-run. The reduced list is what the next invoke loads.

## CrewAI

The `crewai-persistence-*` packages (DynamoDB, MongoDB, SQL, Cosmos DB, Firestore) apply the reducer in `save_state`, and read `memory_namespace` from the flow state. Fall back is `/flow/<uuid>`, so an app that never sets it still gets per-flow memory. In CrewAI the namespace is a `Memory` scope path, so the hook can pass it straight through.

```python
memory = Memory(storage=InMemoryBackend(), embedder=FakeEmbedder(64))

def remember(pruned, namespace):                          # namespace is a scope path
    for m in pruned:
        memory.remember(m["content"], scope=namespace, categories=[m["role"]], importance=0.5)
        # with an LLM: memory.remember_many(memory.extract_memories(text), scope=namespace)

reducer = MessageReducer(config=ReducerConfig(max_messages=6, min_messages=4,
                                              preserve_first=False, on_prune=[remember]))

class SupportState(BaseModel):
    id: str = ""
    memory_namespace: str = "/user/kamal"                 # long-term scope = the user
    messages: list = []

@persist(SQLFlowPersistence(url="sqlite:///flows.db", reducer=reducer))
class SupportFlow(Flow[SupportState]): ...
```

The flow appends ten turns. The persisted state holds four. `memory.recall("moving to Hanoi", scope="/user/kamal")` finds the first one, and nothing lands under `/flow/<uuid>`. The CrewAI-owned pieces here, `remember`, `extract_memories`, `recall`, are the same ones the crew uses for task output. The reducer only adds the second source.

## Strands

Strands is the honest exception. Our session managers (`strands-session-dynamodb`, `-mongodb`, `-sql`) persist the session but do **not** run the reducer, so you call `reduce()` yourself where you prune. Strands' own `ModelExtractor` runs on the live message path through `MemoryManager`, not on what you pass to `add`, so a hook that wants extracted facts from pruned turns has to call an extractor itself.

```python
manager = MemoryManager(stores=[store], injection=False, search_tool_config=False)

def remember(pruned, namespace):
    for m in pruned:                                      # raw turns; add an extractor call if wanted
        asyncio.run(manager.add(text_of(m), {"metadata": {"user": namespace, "role": m["role"]}}))

reducer = MessageReducer(config=ReducerConfig(max_messages=6, min_messages=4,
                                              preserve_first=False, on_prune=[remember]))
agent = Agent(model=model, session_manager=SQLSessionManager(session_id=sid, url="sqlite:///s.db"))

for turn in TURNS:
    agent(turn)
    result = reducer.reduce(existing=agent.messages, namespace="kamal")   # sessions don't do this
    agent.messages[:] = result.surviving
```

You still get the exactly-once delivery, the namespace and the failure containment, because those live in the reducer. What you do not get is the reducer running inside the session save. Trimming `agent.messages` in place bounds the in-process window; the session store keeps the full history unless you also configure a conversation manager. Both are stated in the example.

## PydanticAI

The harness owns long-term memory and expects the model to write it. So the hook does not extract. It appends the pruned turns to a topic file the model can `read_memory` and `search_memory`, using `append_memory` from `pydantic-ai-memory-core`, a compare-and-swap append that coexists with the model's own writes. The backends are `pydantic-ai-dynamodb-memory`, `-cosmosdb-`, `-firestore-` and `-postgres-`.

```python
def remember(pruned, namespace):
    text = "\n".join(f"- {m['content']}" for m in pruned)
    asyncio.run(append_memory(memory, f"{namespace}/main/pruned.md", text))

remember = Background(remember, workers=1)                # worker thread: no running loop there
reducer = MessageReducer(config=ReducerConfig(max_messages=6, min_messages=4,
                                              preserve_first=False, on_prune=[remember]))

result = await agent.run(turn, message_history=prior)
reduced = reducer.reduce(existing=with_roles(result.all_messages()), namespace="kamal")
await history.save(conversation_id, [m["raw"] for m in reduced.surviving])
```

Two limits. The memory side is append-only from the hook: there is no extraction and no consolidation, because the harness reserves that for the model. And the reducer prunes by role (human and ai), while PydanticAI messages carry a `kind` (request and response), so the example wraps each message in a dict with a role before reducing. That is the `with_roles` shim above.

The PydanticAI history side has no framework save path either. `KVHistoryStore` from `pydantic-ai-persistence` is a plain save, so the reduce goes just before it, which is the same place the checkpointers put it.

## What actually ran

Every snippet above is cut from a script that ran offline on the day of writing, with a test that asserts the pruned turns reached the memory side and the window stayed bounded. No API keys, no databases: scripted models, deterministic embedders, in-memory or SQLite stores.

| Example | Test | Key versions |
|---|---|---|
| `langgraph/` (graph, `main.py`) | passed | agentstate-reducer 0.5.0, langgraph 1.2.12, langgraph-memory 0.1.0 |
| `langgraph/` (LangChain `create_agent`, `main_langchain.py`) | passed | langchain 1.4.2, same reducer and engine |
| `crewai/` | passed | crewai 1.15.22, crewai-persistence-sql 0.2.1, crewai-memory-core 0.1.0 |
| `strands/` | passed | strands-agents 1.57.0, strands-session-sql 0.2.0 |
| `pydantic-ai/` | passed | pydantic-ai 2.48.0, pydantic-ai-harness 0.34.0, pydantic-ai-memory-core 0.1.0, pydantic-ai-persistence 0.1.0 |

Reproduce any of them with uv:

```bash
cd missing-link/langgraph      # or crewai, strands, pydantic-ai
uv sync
uv run python main.py
uv run pytest -q
```

The full code is at <https://github.com/skamalj/agentstate-examples/tree/main/missing-link>, one uv project per framework. The package docs, including the extractor snippets for real backends, are at <https://skamalj.github.io/agentstate-reducer/>.

## The point

The two pipes are the right design. Short-term and long-term memory have different owners, different scopes and different lifetimes. The mistake is treating the connection between them as application logic, because application logic does not know when a message is forgotten.

The persistence layer does. It is already computing the boundary every time it saves. Put the hook there, keep the extractor pluggable, and the three questions answer themselves.
