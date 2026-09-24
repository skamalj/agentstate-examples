"""CrewAI: Flow state persistence -> reducer (inside save_state) -> on_prune -> CrewAI Memory.

Zero-LLM: canned turns, ``Memory.remember`` with explicit fields (CrewAI's no-analysis
path), a deterministic embedder, the dict-backed test backend. Swap ``InMemoryBackend``
for ``crewai_memory_postgres.PostgresMemoryBackend`` (or DynamoDB / Cosmos / Firestore)
and ``SQLFlowPersistence(url=...)`` for any of the crewai-persistence-* packages.
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")   # CrewAI prints emoji in its event log

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

from crewai.flow.flow import Flow, listen, start
from crewai.flow.persistence import persist
from crewai.memory import Memory
from crewai_memory_core.testing import FakeEmbedder, InMemoryBackend
from crewai_persistence_sql import SQLFlowPersistence
from pydantic import BaseModel

from agentstate_reducer import MessageReducer, ReducerConfig


def build(db_url: str):
    memory = Memory(storage=InMemoryBackend(), embedder=FakeEmbedder(64), consolidation_threshold=1.0)

    def remember(pruned, namespace):                          # the reducer's on_prune hook
        for m in pruned:                                      # namespace is a CrewAI scope path
            memory.remember(m["content"], scope=namespace, categories=[m["role"]], importance=0.5)
            # with an LLM: memory.remember_many(memory.extract_memories(text), scope=namespace)

    reducer = MessageReducer(config=ReducerConfig(min_messages=4, max_messages=6,
                                                  preserve_first=False, on_prune=[remember]))
    persistence = SQLFlowPersistence(url=db_url, reducer=reducer)

    class SupportState(BaseModel):
        id: str = ""
        memory_namespace: str = "/user/kamal"                 # long-term scope = the user
        messages: list = []

    @persist(persistence)
    class SupportFlow(Flow[SupportState]):
        @start()
        def greet(self):
            self.state.messages += [
                {"id": "h0", "role": "human", "content": "I moved to Hanoi last month"},
                {"id": "a0", "role": "ai", "content": "Welcome to Hanoi!"},
            ]

        @listen(greet)
        def chat(self):
            for i in range(1, 5):
                self.state.messages += [
                    {"id": f"h{i}", "role": "human", "content": f"question {i} about visas"},
                    {"id": f"a{i}", "role": "ai", "content": f"answer {i} about visas"},
                ]

    return SupportFlow, persistence, memory


def run():
    tmp = tempfile.mkdtemp()
    SupportFlow, persistence, memory = build(f"sqlite:///{tmp}/flows.db")
    flow = SupportFlow()
    flow.kickoff()
    memory.drain_writes()

    saved = persistence.load_state(flow.state.id)["messages"]
    hits = memory.recall("moving to Hanoi", scope="/user/kamal", depth="shallow", limit=3)
    leaked = memory.recall("Hanoi", scope=f"/flow/{flow.state.id}", depth="shallow", limit=3)
    memory.close()
    return saved, [h.record.content for h in hits], leaked


if __name__ == "__main__":
    saved, hits, _ = run()
    print(f"persisted flow state holds {len(saved)} messages (short-term, this flow):")
    for m in saved:
        print(f"  {m['role']:>5}: {m['content']}")
    print("recall('moving to Hanoi', scope='/user/kamal'):")
    for c in hits:
        print(f"  - {c}")
