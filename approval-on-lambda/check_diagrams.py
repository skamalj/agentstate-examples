"""Gate: every number and id drawn in the diagrams must still be in blog.md.

The expected values are read from blog.md itself, never from a log or a note, and each one
must appear in the diagram's SVG and (where it matters) in the image's alt text in blog.md.
A diagram that was true when it was drawn and quietly stopped being true fails here.

    uv run python check_diagrams.py            # this folder
    uv run python check_diagrams.py ../other   # another post: its blog.md and images/

The mechanics are generic (grab a value from the prose with a regex, assert the SVG and the
alt text contain it). The RULES table is per post: a rule whose pattern is absent from the
blog is reported as SKIP, so pointing this at another folder runs whatever rules apply and
you add rules for that post's own numbers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

folder = Path(sys.argv[1] if len(sys.argv) > 1 else __file__).resolve()
folder = folder if folder.is_dir() else folder.parent
blog = (folder / "blog.md").read_text(encoding="utf-8")
svgs = {p.stem: p.read_text(encoding="utf-8") for p in (folder / "images").glob("*.svg")}
alts = {m.group(2): m.group(1) for m in re.finditer(r"!\[([^\]]*)\]\(images/([\w-]+)\.png\)", blog)}


def grab(rx: str) -> str | None:
    m = re.search(rx, blog)
    return m.group(1) if m else None


# (diagram, label, expected value from blog.md, must also appear in the alt text)
RULES: list[tuple[str, str, str | None, bool]] = []

# act 4: the four REPORT lines, in order
reports = re.findall(
    r"REPORT Duration: ([\d.]+) ms\s+Billed Duration: (\d+) ms\s+Max Memory Used: (\d+) MB(?:\s+Init Duration: ([\d.]+) ms)?",
    blog,
)
if len(reports) == 4:
    (d1, b1, m1, init), (d2, _, m2, _), (d3, _, m3, _), (d4, _, m4, _) = reports
    RULES += [
        ("four-invocations", "init", init, True),
        ("four-invocations", "duration 1", d1, True),
        ("four-invocations", "billed 1", b1, True),
        ("four-invocations", "duration 2", d2, True),
        ("four-invocations", "duration 3", d3, True),
        ("four-invocations", "duration 4", d4, True),
        ("four-invocations", "memory range", f"{min(m1, m2, m3, m4)}–{max(m1, m2, m3, m4)} MB", False),
    ]
RULES += [
    ("four-invocations", "GB-seconds", grab(r"billed ([\d.]+) GB-seconds"), False),
    ("four-invocations", "no-op billed ms", (lambda m: m and f"{m.group(1)} and {m.group(2)} ms")(re.search(r"\*\*(\d+) and (\d+) milliseconds\*\*", blog)), False),
    # the silent failure: the property that fixes it and the evidence line the run prints
    ("timeout-silent-failure", "queue property", grab(r"`(content_based_deduplication=True)`"), False),
    ("timeout-silent-failure", "schedule DLQ count", (lambda v: v and f"undelivered schedules on the schedule dead-letter queue: {v}")(grab(r"undelivered schedules on the schedule dead-letter queue: (\d+)")), True),
    # act 3: the envelope
    ("envelope", "event_id", grab(r'"event_id": "([A-Z0-9]+)"'), False),
    ("envelope", "thread_id", grab(r'"thread_id": "(order-[0-9a-f-]+)",\n  "question_id"'), False),
    ("envelope", "question_id[:8]", grab(r'"question_id": "([0-9a-f]{8})'), False),
    ("envelope", "expires_at", grab(r'"expires_at": "([^"]+)"'), False),
    ("envelope", "amount", grab(r'"amount": (\d+)'), False),
    # act 2: the parked thread
    ("parked-thread", "PK", grab(r"Query on PK='([^']+)'"), True),
    ("parked-thread", "items", grab(r"Query on PK='[^']+': (\d+) items"), True),
    ("parked-thread", "__interrupt__ bytes", grab(r"channel='__interrupt__', (\d+) bytes"), True),
    ("parked-thread", "items after resume", grab(r"items after the resume: (\d+) \(was"), False),
    # the deadline: the schedule a consumer builds out of expires_at.
    # These were registered before the diagram was drawn and skipped until it
    # landed, which is the behaviour the docstring describes.
    ("timeout-sequence", "schedule name", grab(r'"Name": "(refund-timeout-[0-9a-f]+)"'), False),
    ("timeout-sequence", "at() expression", grab(r'"ScheduleExpression": "(at\([^"]+\))"'), True),
    ("timeout-sequence", "self-deleting", grab(r'"ActionAfterCompletion": "(DELETE)"'), True),
    ("timeout-sequence", "expires_at", grab(r"expires_at  : ([\dTZ:-]+)"), True),
    ("timeout-sequence", "refunds after deadline", grab(r"refunds recorded after the deadline: (\d+)"), False),
    ("timeout-sequence", "refunds after late approval", grab(r"refunds recorded after the late approval: (\d+)"), False),
]
for size in re.findall(r"type='msgpack', (\d+) bytes", blog):
    RULES.append(("parked-thread", "checkpoint bytes", f"{size} bytes", False))
for channel, size in re.findall(r"channel='([^']+)', (\d+) bytes", blog):
    RULES.append(("parked-thread", f"write {channel}", f"{size} bytes", False))
for frag in re.findall(r"checkpoint SK='[0-9a-f]+-([0-9a-f]{4})-", blog):
    RULES.append(("parked-thread", "checkpoint SK fragment", f"…{frag}…", False))

failed = checked = 0
for diagram, label, value, in_alt in RULES:
    if not value or diagram not in svgs:  # None or '' (an unmatched optional group) both mean: nothing to check
        print(f"SKIP {diagram:18} {label:24} (pattern or diagram not present)")
        continue
    checked += 1
    ok_svg = value in svgs[diagram]
    ok_alt = value in alts.get(diagram, "") if in_alt else True
    ok = ok_svg and ok_alt
    failed += not ok
    flag = "y" if ok_svg else "N"
    alt_flag = "-" if not in_alt else ("y" if ok_alt else "N")
    print(f"{'OK  ' if ok else 'DIFF'} {diagram:18} {label:24} blog={value!r:30} svg={flag} alt={alt_flag}")

print(f"\n{checked} checked, {failed} differences")
sys.exit(1 if failed else 0)
