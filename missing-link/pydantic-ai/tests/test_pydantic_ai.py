import asyncio

from main import run


def test_pruned_turns_are_appended_to_the_topic_file():
    window, page, hits = asyncio.run(run("kamal"))
    assert len(window) <= 6
    assert "Hanoi" not in "".join(str(m) for m in window)
    assert "Hanoi" in page and "late flights" in page
    assert page.count("Hanoi") == 1                              # appended once
    assert hits
