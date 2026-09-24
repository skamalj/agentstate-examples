"""End-to-end for the LangChain create_agent variant: same reducer, same hook, recall via engine.tools()."""
import boto3
import pytest

import main
import main_langchain


def _aws_ok():
    try:
        boto3.client("sts").get_caller_identity()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _aws_ok(), reason="AWS credentials not available")


@pytest.fixture(scope="module")
def result():
    return main_langchain.run()


def test_create_agent_checkpoint_is_bounded(result):
    assert len(result["final_window"]) <= main.MAX_MESSAGES
    assert result["total_pruned"] + len(result["final_window"]) == 2 * len(main.TURNS)


def test_create_agent_memories_and_tool_recall(result):
    contents = " ".join(r.content.lower() for r in result["memories"])
    assert "overnight" in contents and "2027" in contents and "meera" in contents
    assert result["tool_outputs"], "the agent must have called search_memory"
    tool_text = " ".join(result["tool_outputs"]).lower()
    assert "overnight" in tool_text and "2027" in tool_text
    a1, a2 = result["answers"]
    assert "overnight" in a1.lower() and "meera" in a1.lower()
    assert "2027" in a2 and "window" in a2.lower()
