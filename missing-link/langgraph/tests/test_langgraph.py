from main import run


def test_pruned_turns_reach_the_store_exactly_once():
    window, memories = run("kamal")
    assert len(window) <= 6                                   # bounded checkpoint
    assert not any("Hanoi" in m.content for m in window)      # the early turn left the window ...
    assert any("Hanoi" in c for c in memories)                # ... and landed in long-term memory
    assert any("late flights" in c for c in memories)
    assert len(memories) == len(set(memories))                # once each
