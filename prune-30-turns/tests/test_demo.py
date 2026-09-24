"""End-to-end: 30 turns through ReducingSaver -> on_prune -> MemoryEngine, then recall in a fresh thread.

Needs AWS credentials for Bedrock (AWS_PROFILE / AWS_DEFAULT_REGION); skipped otherwise.
Runs the whole demo once (about two and a half minutes) and asserts on the result.
"""
import boto3
import pytest

import main as demo


def _aws_ok():
    try:
        boto3.client("sts").get_caller_identity()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _aws_ok(), reason="AWS credentials not available")


@pytest.fixture(scope="module")
def result():
    return demo.run()


def test_checkpoint_is_bounded(result):
    assert len(result["final_window"]) <= demo.MAX_MESSAGES
    # 60 messages produced; every one is either still in the window or was delivered to on_prune once
    assert result["total_pruned"] + len(result["final_window"]) == 2 * len(demo.TURNS)


def test_memories_were_built_from_pruned_turns(result):
    contents = " ".join(r.content.lower() for r in result["memories"])
    assert "meera" in contents
    assert "2027" in contents            # passport expiry, said in turn 11
    assert "overnight" in contents       # the corrected preference from turn 17


def test_correction_consolidated_the_old_preference(result):
    # no surviving memory still states the morning preference without the overnight correction
    stale = [r.content for r in result["memories"] if "morning" in r.content.lower() and "overnight" not in r.content.lower()]
    assert stale == []
    # and the user's own window-seat preference was not merged away into Meera's aisle-seat memory
    assert any("window" in r.content.lower() and "the user" in r.content.lower() for r in result["memories"])


def test_recall_in_fresh_thread_answers_from_pruned_turns(result):
    first, second = result["recalls"]
    assert any("overnight" in m.record.content.lower() for m in first)
    assert any("meera" in m.record.content.lower() for m in first)
    assert any("2027" in m.record.content for m in second)
    assert any("window" in m.record.content.lower() for m in second)
    a1, a2 = result["answers"]
    assert "overnight" in a1.lower() and "meera" in a1.lower()
    assert "2027" in a2 and "window" in a2.lower()
