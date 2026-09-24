from main_langchain import run


def test_create_agent_prunes_into_the_store():
    window, memories = run("kamal")
    assert len(window) <= 6
    assert not any("Hanoi" in m.content for m in window)
    assert any("Hanoi" in c for c in memories)
    assert any("late flights" in c for c in memories)
    assert len(memories) == len(set(memories))
