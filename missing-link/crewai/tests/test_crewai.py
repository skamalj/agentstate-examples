from main import run


def test_flow_prunes_into_crewai_memory():
    saved, hits, leaked = run()
    assert len(saved) <= 6                                     # bounded flow state
    assert saved[-1]["content"] == "answer 4 about visas"
    assert hits and "Hanoi" in hits[0]                         # pruned turn is recallable for the user
    assert leaked == []                                        # nothing under the per-flow fallback scope
