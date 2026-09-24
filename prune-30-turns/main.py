"""We ran the pruner for 30 turns. Here is what the agent remembered.

One user, one long travel-planning conversation, a bounded checkpoint window
(max 10 / min 6 messages) and long-term memory built from the pruned turns.
Then a fresh thread with the same memory namespace asks about things that only
ever existed in pruned turns.

Stack: LangGraph InMemorySaver wrapped in agentstate_reducer.langgraph.ReducingSaver,
LangGraph InMemoryStore with Bedrock Titan embeddings, langgraph_memory.MemoryEngine
with a Bedrock chat model.
"""
from __future__ import annotations

import os
import sys
import time
from uuid import uuid4

from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.store.memory import InMemoryStore

from agentstate_reducer import MessageReducer, ReducerConfig
from agentstate_reducer.langgraph import ReducingSaver
from langgraph_memory import MemoryConfig, MemoryEngine
from langgraph_memory.engine import LLMConsolidator, LLMExtractor

CHAT_MODEL = os.environ.get("BEDROCK_CHAT_MODEL", "global.anthropic.claude-sonnet-4-6")
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
MAX_MESSAGES, MIN_MESSAGES = 10, 6
# Titan Embed v2 scores paraphrases of the same fact around 0.45-0.7 and unrelated facts
# below 0.25 (see probe in README), so the engine's default 0.85 never triggers consolidation.
CONSOLIDATION_THRESHOLD = float(os.environ.get("CONSOLIDATION_THRESHOLD", "0.4"))
RECALL_LIMIT = 8
USER_ID = "asha"
NAMESPACE = ("memories", USER_ID)

EXTRACT_PROMPT = (
    "You extract long-term memories from a conversation transcript between the user (Asha, lines marked "
    "'human') and an assistant (lines marked 'ai'). Return only durable facts, preferences, decisions and "
    "commitments about the user or their trip that a future conversation would benefit from. Write each as one "
    "short, self-contained sentence in the third person, attributing it correctly ('the user' or a named person). "
    "Skip greetings, transient state, general travel advice and anything already implied by another fact. "
    "Give short lowercase category tags and an importance from 0 (trivia) to 1 (must never forget)."
)

CONSOLIDATE_PROMPT = (
    "You maintain a set of long-term memories about a user. Given a NEW fact and the most similar EXISTING "
    'memories, decide: "skip" if the new fact is already captured; "update" (with target_key and merged content) '
    "only if the new fact is about the same subject as one existing memory, meaning the same person AND the same "
    "attribute (for example the same person's flight-timing preference). A fact about a different person, or a "
    'different attribute of the same person, is "insert", however similar the wording. When the new fact corrects '
    "an existing memory, the merged content must state the current truth first and note what changed."
)

SYSTEM = ("You are a concise travel-planning assistant. Answer in one or two short sentences. "
          "Do not repeat the user's preferences back as a list.")

# 30 human turns: preferences, facts, one correction (turn 17 overrides turn 4).
TURNS = [
    "Hi! I want to plan a two-week trip to Japan in the second week of November.",
    "I'll be flying out of Bangalore, BLR is my home airport.",
    "I'm travelling with my sister Meera, so two adults.",
    "I strongly prefer morning departures, I hate arriving anywhere at night.",
    "Budget for the two of us is about 4 lakh rupees, flights included.",
    "We're both vegetarian, and I'm allergic to peanuts, so food matters a lot.",
    "Which cities would you suggest for a first trip? We like temples and food markets more than nightlife.",
    "Tokyo, Kyoto and Osaka sounds right. How many nights in each?",
    "Is the JR Pass still worth it for that route?",
    "I always book window seats, Meera takes the aisle.",
    "My passport expires in March 2027. Is that a problem for a November trip?",
    "Do Indians need a visa for Japan? We have never been.",
    "What's the weather like in Kyoto in mid-November?",
    "I'd like one night in a traditional ryokan with an onsen, ideally in Hakone.",
    "We don't drink alcohol, so skip the sake tours.",
    "Meera gets motion sick on buses, so trains over buses wherever possible.",
    "Actually, scrap what I said about morning flights. I've thought about it and an overnight flight is better so we don't lose a day. Book overnight departures from now on.",
    "Which airlines fly overnight from BLR to Tokyo, direct or one stop?",
    "I'd rather connect in Singapore than in Bangkok if there is a choice.",
    "How much cash should we carry versus cards in Japan?",
    "I want to see the autumn leaves. Is Arashiyama good in that week?",
    "Are there good vegetarian ramen places in Tokyo? Shojin ryori too.",
    "We'd like to do a day trip to Nara for the deer park.",
    "Can you suggest a rough day-by-day outline for the Kyoto part?",
    "Is TeamLab in Tokyo worth booking ahead?",
    "What's a reasonable hotel budget per night in Tokyo for two, mid-range?",
    "Meera wants to try a tea ceremony in Kyoto. Where?",
    "How early should we reach Narita for the return flight?",
    "Any etiquette things we should know before we go?",
    "Great, that's enough for today. Thank you!",
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "..."


def run() -> dict:
    """Run the whole demo, printing as it goes; return the results for tests."""
    sys.stdout.reconfigure(encoding="utf-8")
    llm = ChatBedrockConverse(model=CHAT_MODEL, temperature=0, max_tokens=300)
    embed = BedrockEmbeddings(model_id=EMBED_MODEL)
    store = InMemoryStore(index={"dims": 1024, "embed": embed, "fields": ["content"]})
    base_extractor = LLMExtractor(llm, prompt=EXTRACT_PROMPT)
    retries = {"count": 0}

    def extractor(text: str):
        # structured output occasionally comes back empty for a chunk that clearly has facts; retry once
        facts = base_extractor(text)
        if not facts and text.count("human:") >= 2:
            retries["count"] += 1
            facts = base_extractor(text)
        return facts

    engine = MemoryEngine(store, llm, extractor=extractor,
                          consolidator=LLMConsolidator(llm, prompt=CONSOLIDATE_PROMPT),
                          config=MemoryConfig(consolidation_threshold=CONSOLIDATION_THRESHOLD))

    # ---- the on_prune hook: what engine.on_prune does, plus logging of what was stored
    known_keys: set[str] = set()
    prune_events: list[dict] = []

    def remember_and_log(pruned, namespace):
        left = [f"{m.type}: {short(m.content, 60)}" for m in pruned]
        written = engine.remember(pruned, namespace)
        stored = []
        for rec in written:
            verb = "UPDATED" if rec.key in known_keys else "NEW"
            known_keys.add(rec.key)
            stored.append(f"{verb} [{rec.key[:8]}] {rec.content}  (importance={rec.importance:.2f}, {','.join(rec.categories)})")
        prune_events.append({"left": left, "stored": stored, "namespace": namespace})

    reducer = MessageReducer(config=ReducerConfig(
        max_messages=MAX_MESSAGES, min_messages=MIN_MESSAGES, preserve_first=False,
        on_prune=[remember_and_log],
    ))
    saver = ReducingSaver(InMemorySaver(), reducer)

    # ---- the agent graph for the long conversation
    def agent(state: MessagesState):
        reply = llm.invoke([SystemMessage(SYSTEM), *state["messages"]])
        return {"messages": [AIMessage(content=reply.content, id=str(uuid4()))]}

    b = StateGraph(MessagesState)
    b.add_node("agent", agent)
    b.add_edge(START, "agent")
    b.add_edge("agent", END)
    graph = b.compile(checkpointer=saver)

    thread_a = f"trip-{uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread_a, "memory_namespace": NAMESPACE}}

    log(f"chat model      : {CHAT_MODEL}")
    log(f"embedding model : {EMBED_MODEL} (dims=1024)")
    log(f"window          : max_messages={MAX_MESSAGES} min_messages={MIN_MESSAGES}")
    log(f"consolidation   : threshold={CONSOLIDATION_THRESHOLD}, custom same-subject prompt")
    log(f"recall limit    : {RECALL_LIMIT}")
    log(f"thread A        : {thread_a}   namespace={NAMESPACE}")
    log()

    t0 = time.time()
    total_pruned = 0
    for i, text in enumerate(TURNS, 1):
        before = len(prune_events)
        out = graph.invoke({"messages": [HumanMessage(content=text, id=f"h{i:02d}")]}, config=cfg)
        stored_now = saver.get_tuple(cfg).checkpoint["channel_values"]["messages"]
        log(f"--- turn {i:02d} | user: {short(text)}")
        log(f"    agent: {short(out['messages'][-1].content, 110)}")
        log(f"    checkpoint holds {len(stored_now)} messages")
        for ev in prune_events[before:]:
            total_pruned += len(ev["left"])
            log(f"    PRUNED {len(ev['left'])} messages -> on_prune(namespace={ev['namespace']}):")
            for line in ev["left"]:
                log(f"        left window : {line}")
            if ev["stored"]:
                for line in ev["stored"]:
                    log(f"        memory      : {line}")
            else:
                log("        memory      : (nothing durable extracted)")
    elapsed = time.time() - t0

    final = saver.get_tuple(cfg).checkpoint["channel_values"]["messages"]
    memories = engine.list(NAMESPACE)
    log()
    log("=== after 30 turns ===")
    log(f"messages produced        : {2 * len(TURNS)}")
    log(f"messages in checkpoint   : {len(final)}")
    log(f"messages delivered to on_prune (exactly once each): {total_pruned}")
    log(f"prune events             : {len(prune_events)}")
    log(f"memories in {NAMESPACE}: {len(memories)}")
    log(f"wall time for 30 turns   : {elapsed:.0f}s")
    log(f"empty extractions retried: {retries['count']}")
    log()
    log("=== everything the engine holds for this user ===")
    for rec in sorted(memories, key=lambda r: r.created_at):
        log(f"  [{rec.key[:8]}] {rec.content}  (importance={rec.importance:.2f}, {','.join(rec.categories)})")

    # ---- phase 2: a fresh thread, same user, question answerable only from pruned turns
    log()
    log("=== fresh thread, same memory_namespace ===")

    class State(MessagesState):
        memory: str

    recalls: list[list] = []
    answers: list[str] = []

    def recall_then_answer(state: State):
        # what engine.recall_node does, with the ranked matches printed
        query = state["messages"][-1].content
        matches = engine.recall(query, NAMESPACE, limit=RECALL_LIMIT)
        log(f"recall(query={query!r}, limit={RECALL_LIMIT}):")
        for m in matches:
            log(f"  score={m.score:.3f} sim={m.similarity:.3f}  {m.record.content}")
        recalls.append(matches)
        memory = "\n".join(m.format() for m in matches)
        return {"memory": memory}

    def agent_with_memory(state: State):
        sys_prompt = SYSTEM + "\n\nWhat you remember about this user from earlier conversations:\n" + state["memory"]
        reply = llm.invoke([SystemMessage(sys_prompt), *state["messages"]])
        return {"messages": [AIMessage(content=reply.content, id=str(uuid4()))]}

    b2 = StateGraph(State)
    b2.add_node("recall", recall_then_answer)
    b2.add_node("agent", agent_with_memory)
    b2.add_edge(START, "recall")
    b2.add_edge("recall", "agent")
    b2.add_edge("agent", END)
    saver_b = ReducingSaver(InMemorySaver(), reducer)
    graph2 = b2.compile(checkpointer=saver_b)

    thread_b = f"followup-{uuid4().hex[:8]}"
    cfg2 = {"configurable": {"thread_id": thread_b, "memory_namespace": NAMESPACE}}
    log(f"thread B: {thread_b}   (new saver, empty checkpoint; only the namespace is shared)")
    questions = [
        "I'm back. Remind me: what flight timing did I settle on, and who is coming with me?",
        "Also, when does my passport expire, and which seat should you book for me?",
    ]
    for q in questions:
        log()
        log(f"user : {q}")
        out = graph2.invoke({"messages": [HumanMessage(content=q, id=str(uuid4()))]}, config=cfg2)
        answers.append(out["messages"][-1].content)
        log(f"agent: {out['messages'][-1].content}")

    log()
    log("=== sanity ===")
    log(f"thread B checkpoint holds {len(saver_b.get_tuple(cfg2).checkpoint['channel_values']['messages'])} messages "
        f"(its own two turns); thread A's saver has no entry for thread B: {saver.get_tuple(cfg2) is None}")
    return {
        "final_window": final, "memories": memories, "prune_events": prune_events,
        "total_pruned": total_pruned, "recalls": recalls, "answers": answers,
    }


if __name__ == "__main__":
    run()
