from main import run


def test_pruned_turns_reach_the_memory_store():
    window, memories, store = run("kamal")
    assert len(window) <= 6
    assert not any("Hanoi" in str(m) for m in window)
    assert any("Hanoi" in c for c in memories)
    assert any("late flights" in c for c in memories)
    assert all(r["metadata"]["user"] == "kamal" for r in store.rows)
    assert len(store.rows) == len({r["content"] for r in store.rows})   # once each
