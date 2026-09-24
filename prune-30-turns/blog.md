# We ran the pruner for 30 turns. Here is what the agent remembered.

Most LangGraph agents have a checkpoint that only grows. Every turn appends two messages, every turn re-sends the whole thing to the model, and nothing ever leaves. The usual fix is to trim the list before the model call, which throws away the old turns for good.

We wanted the other thing: a checkpoint that stays small, and a long-term memory that is built *from* the turns that fall out of it. Then, in a brand-new thread, the agent should still know what it was told twenty turns ago.

This post is one run of exactly that. One user, one 30-turn conversation, a window of at most 10 messages, and a fresh thread at the end. Every number and every line of output below is pasted from that run. The full project, with the log and a pytest that asserts the result, is at [github.com/skamalj/agentstate-examples/tree/main/prune-30-turns](https://github.com/skamalj/agentstate-examples/tree/main/prune-30-turns).

## The pieces

Three packages, all from PyPI, plus AWS Bedrock for the models.

- `agentstate-reducer` 0.5.0 gives us `MessageReducer`, and, new in 0.5.0, `ReducingSaver`, which wraps any LangGraph checkpointer and runs the reducer on every write.
- `langgraph-memory` 0.1.0 gives us `MemoryEngine`: extract facts from text with an LLM, consolidate them against what is already stored, and recall them ranked by similarity, recency and importance.
- LangGraph's own `InMemorySaver` and `InMemoryStore`, so the demo has no infrastructure. The same code runs on the DynamoDB or Postgres savers and stores documented at [skamalj.github.io/agentstate-reducer](https://skamalj.github.io/agentstate-reducer/).

The models are Claude Sonnet 4.6 and Titan Embed v2 (1024 dimensions) on Bedrock in ap-south-1.

## The wiring

The store has to be built with an index, because the engine never embeds anything itself.

```python
store = InMemoryStore(index={"dims": 1024, "embed": embed, "fields": ["content"]})
engine = MemoryEngine(store, llm, extractor=extractor,
                      consolidator=LLMConsolidator(llm, prompt=CONSOLIDATE_PROMPT),
                      config=MemoryConfig(consolidation_threshold=CONSOLIDATION_THRESHOLD))
```

The extractor and consolidator arguments are the engine's own LLM classes with custom prompts, and the threshold is 0.4. Both are explained at the end; they were not our first choice.

The reducer keeps at most 10 messages and prunes down to 6. Whatever leaves the window is handed to the hooks in `on_prune`, and each message id reaches a hook exactly once.

```python
reducer = MessageReducer(config=ReducerConfig(
    max_messages=10, min_messages=6, preserve_first=False,
    on_prune=[remember_and_log],
))
saver = ReducingSaver(InMemorySaver(), reducer)
graph = builder.compile(checkpointer=saver)
```

The hook is `engine.remember(pruned, namespace)` with some logging around it. That is all `engine.on_prune` does too; we inlined it so we could print what got stored. The namespace comes from the run config:

```python
cfg = {"configurable": {"thread_id": thread_a, "memory_namespace": ("memories", "asha")}}
```

In production you would wrap the hook in `Background(...)` so the LLM extraction happens off the request path. Here it runs inline so the log lines up with the turns.

## The conversation

Asha is planning a Japan trip with her sister. Over 30 turns she states preferences, mentions facts, and, at turn 17, reverses something she said at turn 4.

```text
turn 04  I strongly prefer morning departures, I hate arriving anywhere at night.
turn 10  I always book window seats, Meera takes the aisle.
turn 11  My passport expires in March 2027. Is that a problem for a November trip?
turn 17  Actually, scrap what I said about morning flights. ... Book overnight departures from now on.
```

Nothing in the agent prompt mentions memory. It is a two-sentence travel assistant.

## Turn by turn

The first five turns fill the window. At turn 6 the checkpoint would hold 12 messages, so the reducer prunes.

```text
--- turn 06 | user: We're both vegetarian, and I'm allergic to peanuts, so food matters a...
    checkpoint holds 6 messages
    PRUNED 5 messages -> on_prune(namespace=('memories', 'asha')):
        left window : human: Hi! I want to plan a two-week trip to Japan in the second w...
        left window : ai: Great timing — mid-November is perfect for fall foliage in ...
        left window : human: I'll be flying out of Bangalore, BLR is my home airport.
        left window : ai: There are several good options from BLR, including direct o...
        left window : human: I'm travelling with my sister Meera, so two adults.
        memory      : NEW [6332fe7e] The user is planning a two-week trip to Japan in the second week of November.  (importance=0.90, travel,decision)
        memory      : NEW [8fcc2b6b] The user's home airport is Bangalore (BLR).  (importance=0.90, personal,travel)
        memory      : NEW [fb435e19] The user is travelling with their sister Meera — a party of two adults.  (importance=0.80, personal,travel)
    PRUNED 1 messages -> on_prune(namespace=('memories', 'asha')):
        left window : ai: Travelling as a pair makes things easy — you can share acco...
```

Two prune events per turn, five messages and then one, is not a bug. LangGraph writes a checkpoint after the input and again after the node. The first write sees 11 messages and prunes to 6. The second write is built from the graph's in-process state, which still has all 12, so the reducer prunes 6 again, but 5 of those ids were already delivered. The exactly-once bookkeeping hands the hook only the one it has not seen.

Plot the checkpoint size after every turn and the whole conversation becomes a sawtooth. The blue line climbs by two each turn, hits 12, and snaps back to 6; each orange drop is one of those snaps, and each one hands a batch of turns to the hook.

![Checkpoint size over 30 turns: +2 per turn, pruned from 12 back to 6 at turn 6 and every third turn after. Each orange drop hands six turns to on_prune, once each, into the ('memories', 'asha') namespace: 9 prunes × 6 = 54 delivered, 28 memories after turn 30. Turn 9 stores the morning-flight preference; turn 21 updates it with the turn-17 correction.](images/sawtooth-30-turns.png)

Nine orange drops, six messages each: 9 × 6 = 54, which is the delivered count you will see in the summary at the end. The log books those same 54 as 18 events, because every drop is the five-then-one pair described above. Two of the drops are labelled, turn 9 and turn 21, and they are the two ends of the story we care about. The checkpoint itself just cycles 6, 8, 10, 6 for the rest of the conversation. At turn 9 the morning-flight preference leaves the window and becomes a memory:

```text
--- turn 09 | user: Is the JR Pass still worth it for that route?
    checkpoint holds 6 messages
    PRUNED 5 messages -> on_prune(namespace=('memories', 'asha')):
        left window : human: I strongly prefer morning departures, I hate arriving anywh...
        ...
        memory      : NEW [f7cea1b4] The user strongly prefers morning departures and dislikes arriving at destinations at night.  (importance=0.80, preference,travel)
        memory      : NEW [d45dfb17] The user's total trip budget for two people is approximately 4 lakh rupees (roughly $2,400 USD), including flights.  (importance=0.90, budget,travel)
        memory      : NEW [fbc4d3dc] The user and their travel companion are both vegetarian.  (importance=0.90, preference,food,personal)
        memory      : NEW [38598c5c] The user is allergic to peanuts.  (importance=1.00, health,food,personal)
```

Note the importance the extractor gave the peanut allergy.

## The consolidation moment

Turn 17 is the correction. It leaves the window at turn 21. The engine searches the store for similar memories, finds the turn-9 one, and asks the consolidator what to do. Same key, new content:

```text
--- turn 21 | user: I want to see the autumn leaves. Is Arashiyama good in that week?
    PRUNED 5 messages -> on_prune(namespace=('memories', 'asha')):
        left window : human: Meera gets motion sick on buses, so trains over buses where...
        left window : ai: Good news — Hakone has excellent rail options like the Roma...
        left window : human: Actually, scrap what I said about morning flights. I've tho...
        left window : ai: Noted — I'll factor in overnight departures for any flights...
        left window : human: Which airlines fly overnight from BLR to Tokyo, direct or o...
        memory      : NEW [1da19c5b] Meera gets motion sick on buses and prefers trains over buses wherever possible.  (importance=0.90, personal,preference,travel)
        memory      : UPDATED [f7cea1b4] The user prefers overnight flights over morning flights so as not to lose a day of travel. Previously, the user strongly preferred morning departures and disliked arriving at destinations at night.  (importance=0.85, decision,preference,travel)
```

The old memory is gone. There is one record for flight timing, it says overnight, and it keeps a note of what changed.

That outcome hinged on a single number. Here is the decision the engine made at turn 21, and the decision it would have made with the library default.

![Consolidation at turn 21: the turn-4 preference became memory f7cea1b4 at turn 9; the turn-17 correction is extracted as a candidate with cosine similarity 0.46. With consolidation_threshold 0.4 the engine updates the same record (28 memories after 30 turns); with the default 0.85 it would insert a second, conflicting record (53 memories).](images/consolidation-turn-21.png)

The fork is at 0.46, the cosine similarity Titan v2 assigns to the two phrasings. Our threshold of 0.4 sits below it, so the candidate is treated as the same memory and the consolidator gets to decide, which is the green path and the update you just read. The default of 0.85 sits far above it, so the engine never asks the consolidator at all and inserts a second, contradicting record. That is the grey path, and it is what our first run did: 53 memories instead of 28, with the morning and overnight preferences sitting side by side. More on how we found that number at the end.

## After 30 turns

```text
=== after 30 turns ===
messages produced        : 60
messages in checkpoint   : 6
messages delivered to on_prune (exactly once each): 54
prune events             : 18
memories in ('memories', 'asha'): 28
wall time for 30 turns   : 128s
```

Sixty messages were produced. Six are in the checkpoint, fifty-four went through the hook, and the sum is sixty. Twenty-eight memories came out of it, including these:

```text
[bb3d69df] The user always books window seats when flying or on trains.  (importance=0.80, preference,travel)
[b6c8c887] Meera always takes the aisle seat.  (importance=0.70, preference,travel,personal)
[30359a01] The user's passport expires in March 2027.  (importance=0.90, personal,travel)
[38598c5c] The user is allergic to peanuts.  (importance=1.00, health,food,personal)
[c6a61679] The user does not drink alcohol and should not be recommended sake tours or alcohol-related activities.  (importance=0.90, preference,personal)
```

Not all of it is tidy. "Travelling with their sister Meera, a party of two adults" and "travelling to Japan as a pair" both survived, and the extractor guessed that Meera is "likely Indian". A 0.4 threshold merges corrections but not every paraphrase.

## A fresh thread

Now a new thread, a new `InMemorySaver`, an empty checkpoint. Only the namespace is the same. The graph gets one extra node in front of the agent: recall for the incoming message and put the matches in the system prompt. Laid out end to end, the two threads look like this.

![Thread A saves through ReducingSaver; 54 pruned messages go once each to on_prune → engine.remember → an indexed InMemoryStore under ('memories', 'asha'). Thread B starts with an empty saver; a recall node queries the same namespace and puts the matches in the system prompt. The threads share nothing but the namespace.](images/fresh-thread-recall.png)

Follow the arrows. Thread A's saver holds six messages; the 54 that left it went through the hook into the store, keyed by the user rather than by the thread. Thread B has its own saver, and it is empty. The only line connecting the two halves of the picture is the dashed query into the store and the matches coming back out. In code, the recall node is one call:

```python
matches = engine.recall(query, ("memories", "asha"), limit=8)
```

The first question needs turn 3 and turn 17. Both are long gone from any checkpoint.

```text
user : I'm back. Remind me: what flight timing did I settle on, and who is coming with me?
recall(query=..., limit=8):
  score=0.627 sim=0.314  The user prefers overnight flights over morning flights so as not to lose a day of travel. Previously, the user strongly preferred morning departures and disliked arriving at destinations at night.
  score=0.582 sim=0.244  The user's trip involves flying out of BLR (Bengaluru) to Tokyo.
  score=0.573 sim=0.187  The user's Japan trip covers the Tokyo-Kyoto-Osaka route, and this is their first trip to Japan.
  score=0.570 sim=0.180  The user's home airport is Bangalore (BLR).
  score=0.561 sim=0.162  The user is Indian and holds an Indian passport. Meera is also likely Indian with an Indian passport.
  score=0.557 sim=0.194  The user is travelling with their sister Meera — a party of two adults.
  score=0.551 sim=0.181  The user always books window seats when flying or on trains.
  score=0.550 sim=0.139  Meera gets motion sick on buses and prefers trains over buses wherever possible.
agent: Your records show a bit of a conflict on flight timing — you've expressed both a preference for overnight flights and a preference for morning departures, so it's worth double-checking which you actually settled on. You're travelling with your sister Meera.
```

Recall did its job: the merged flight memory is the top hit and Meera is in the list. The answering model then hedged, because the merged record mentions both preferences. The memory is right and unambiguous ("prefers overnight ... Previously ... morning"); the model read the "previously" clause as a live conflict. If you want crisp answers, have the consolidator write only the current truth and keep history in metadata.

The second question needs turns 10 and 11.

```text
user : Also, when does my passport expire, and which seat should you book for me?
recall(query=..., limit=8):
  score=0.762 sim=0.564  The user's passport expires in March 2027.
  score=0.594 sim=0.269  The user always books window seats when flying or on trains.
  ...
agent: Your passport expires in March 2027, and you always prefer a window seat.
```

And the sanity line at the end, so nobody suspects the old checkpoint leaked in:

```text
thread B checkpoint holds 4 messages (its own two turns); thread A's saver has no entry for thread B: True
```

## The same thing with LangChain's create_agent

The graph above is hand-built. If you use LangChain's `create_agent` instead, nothing about the reducer changes, because `create_agent` takes a checkpointer and `ReducingSaver` is one.

```python
agent = create_agent(llm, tools=[], system_prompt=SYSTEM, checkpointer=saver)
```

We ran the identical 30 turns through it, with the same engine and the same hook imported from the first script. The window numbers are the same to the digit:

```text
=== after 30 turns (create_agent) ===
messages produced        : 60
messages in checkpoint   : 6
messages delivered to on_prune (exactly once each): 54
prune events             : 18
memories in ('memories', 'asha'): 30
```

For the fresh thread we did it the agent way this time: no recall node, just `tools=engine.tools()` and one sentence in the system prompt telling it to search memory first. The agent decided to split the compound question into two searches on its own.

```text
user : I'm back. Remind me: what flight timing did I settle on, and who is coming with me?
tool call: search_memory({'query': 'flight timing'})
tool call: search_memory({'query': 'travel companions'})
  - (score=0.64) The user prefers overnight flights over morning flights so as not to lose a day of travel. This is a change from a previous preference...
  ...
  - (score=0.62) The user is travelling to Japan with their sister Meera — a party of two adults. [travel, personal]
agent: You settled on **overnight flights** to avoid losing a day of travel, and you're going with your sister **Meera**.

user : Also, when does my passport expire, and which seat should you book for me?
tool call: search_memory({'query': 'passport expiry date'})
tool call: search_memory({'query': 'seat preference'})
  - (score=0.83) The user's passport expires in March 2027. [personal, travel]
  ...
  - (score=0.62) The user always books window seats when flying or on trains. [preference, travel]
agent: Your passport expires in **March 2027**, so you're fine for a November trip. Book you a **window seat** (and Meera will want the aisle).
```

Two focused queries beat one compound one, and the agent did not hedge on the merged flight memory this time. Same store, same records, different retrieval path.

## What we had to change to get here

This was not the first run. Three settings differ from the defaults, and each one came from a failure we could see in the log.

**Consolidation threshold 0.4, not 0.85.** With the default, the turn-17 correction was inserted next to the turn-4 preference instead of replacing it, and the store ended up with 53 memories instead of 28. Titan Embed v2 simply scores lower than the default assumes. We measured the cosine between the two phrasings of the flight preference at 0.46, between two paraphrases of "planning a trip to Japan" at 0.72, and between unrelated facts at 0.06 to 0.23. The threshold is a property of your embedding model. Probe it before you trust the default.

**An extraction prompt that names the user.** With the stock prompt, the chunk that left the window at turn 21 opened with "Meera gets motion sick on buses", and the extractor attributed everything after it to Meera: "Meera prefers overnight flight departures". Telling the extractor who the `human` speaker is fixed the attribution.

**A consolidation prompt with a same-subject rule.** In the run before this one, the extractor produced "The user always books window seats" and, in the same batch, "Meera takes the aisle". The consolidator saw the second as similar to the first and *updated* it, and the user's window-seat preference was overwritten by her sister's aisle-seat preference. The fresh thread then answered "aisle seat". The prompt now says an update requires the same person and the same attribute, and the test asserts the window-seat memory survives.

We also raised the recall limit from 5 to 8, because a compound question ("what timing, and who is coming") splits the embedding's attention and the Meera memory fell just outside the top 5.

None of these are exotic. They are the knobs you would expect to turn for any extraction-plus-consolidation memory. The point of the run is that the checkpoint side needed no tuning at all: 30 turns, a hard ceiling of 10 messages, every pruned message delivered once.

## Reproduce it

```bash
git clone https://github.com/skamalj/agentstate-examples
cd agentstate-examples/prune-30-turns
uv venv -p 3.12
uv sync
export AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=ap-south-1
uv run python main.py | tee run.log
uv run python main_langchain.py | tee run-langchain.log
uv run pytest -q
```

The dependencies were added with `uv add "agentstate-reducer[langgraph]==0.5.0" "langgraph-memory==0.1.0" langchain-aws boto3` and `uv add --dev pytest`. The tests run each demo once and assert the window bound, the exactly-once count, the consolidation of the corrected preference, the survival of the window-seat memory, and the recall in the fresh thread (via the recall node in one, via the search_memory tool in the other). Each invocation is a separate run with a live model, so the wording in your log will differ from the one quoted here. Ours:

```text
......                                                                   [100%]
6 passed in 282.31s (0:04:42)
```
