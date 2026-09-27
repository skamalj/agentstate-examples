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

import html
import re
import sys
from pathlib import Path

folder = Path(sys.argv[1] if len(sys.argv) > 1 else __file__).resolve()
folder = folder if folder.is_dir() else folder.parent
blog = (folder / "blog.md").read_text(encoding="utf-8")
svgs = {p.stem: p.read_text(encoding="utf-8") for p in (folder / "images").glob("*.svg")}
# Line-anchored and greedy: a "[^\]]*" alt pattern stops at the first "]" inside a caption and
# silently drops that caption (it missed 9 of 10 on another post). A dropped caption reads as
# "not inserted", never as a match.
alts = {m.group(2): m.group(1) for m in re.finditer(r"^!\[(.*)\]\(images/([\w-]+)\.png\)\s*$", blog, re.M)}


def grab(rx: str) -> str | None:
    m = re.search(rx, blog)
    return m.group(1) if m else None


# (diagram, label, expected value from blog.md, must also appear in the alt text)
RULES: list[tuple[str, str, str | None, bool]] = []

# 2026-09-27: the four-invocations and timeout-silent-failure figures were cut with the benchmark
# and the build-diary sections, so their rules are gone rather than left to SKIP forever.
RULES += [
    # step 3: the envelope
    ("envelope", "event_id", grab(r'"event_id": "([A-Z0-9]+)"'), False),
    ("envelope", "thread_id", grab(r'"thread_id": "(order-[0-9a-f-]+)",\n  "question_id"'), False),
    ("envelope", "question_id[:8]", grab(r'"question_id": "([0-9a-f]{8})'), False),
    ("envelope", "expires_at", grab(r'"expires_at": "([^"]+)"'), False),
    ("envelope", "amount", grab(r'"amount": (\d+)'), False),
    # step 2: the parked thread
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
    # Match against the artwork only: the aria-label is a description, and since it equals the
    # caption it would otherwise satisfy "appears in the SVG" for any value the caption states,
    # drawn or not (2026-09-27).
    ok_svg = value in re.sub(r'aria-label="[^"]*"', "", svgs[diagram])
    ok_alt = value in alts.get(diagram, "") if in_alt else True
    ok = ok_svg and ok_alt
    failed += not ok
    flag = "y" if ok_svg else "N"
    alt_flag = "-" if not in_alt else ("y" if ok_alt else "N")
    print(f"{'OK  ' if ok else 'DIFF'} {diagram:18} {label:24} blog={value!r:30} svg={flag} alt={alt_flag}")

# Retired vocabulary: a term the post has renamed away must not survive in any SVG or caption.
# The rules above only ask "is this value present"; this is the opposite shape. Add a term here
# whenever the post renames something (2026-09-27: "Act N" became "Step N").
RETIRED = ["act 1", "act 2", "act 3", "act 4", "acts 2",
           "escalate_refund"]  # 0.8.0: one tool with @wait(when=...), the second tool is gone
for term in RETIRED:
    checked += 1
    hits = [(name, where) for name, body in sorted(svgs.items())
            for where, blob in (("svg", body), ("alt", alts.get(name, "")))  # raw svg: text AND aria-label
            if re.search(r"\b" + re.escape(term) + r"\b", blob, re.I)]
    for name, where in hits:
        print(f"DIFF {name:18} {'retired term':24} {term!r} still in the {where}")
    failed += bool(hits)

# Third leg: the run logs against the post's pasted blocks. The rules above compare post to
# figure, so when prose and caption drift from a log together they agree and nothing fires
# (2026-09-27: an __interrupt__ size of 496 survived two re-syncs that way). Here a value is
# read from the log that produced it and must appear verbatim inside a fenced block of the
# post. Blocks only: prose paraphrases numbers and would fail falsely. Derived figures with
# no log source (the GB-seconds total is arithmetic) are deliberately not pinned.
blocks = "\n".join(re.findall(r"```[^\n]*\n(.*?)```", blog, re.S))
LOG_RULES: list[tuple[str, str, str]] = [  # (log file, regex, label); the whole match must be in a block
    ("run-step2.log", r"Query on PK='[^']+': \d+ items", "step 2 query line"),
    ("run-step2.log", r"type='msgpack', \d+ bytes", "step 2 checkpoint size"),
    ("run-step2.log", r"channel='[^']+', \d+ bytes", "step 2 write size"),
    ("run-step2.log", r"items after the resume: \d+ \(was \d+\)", "step 2 item count after resume"),
    ("run-step3.log", r'"event_id": "[A-Z0-9]+"', "step 3 event_id"),
    ("run-step3.log", r'"thread_id": "order-[0-9a-f-]+"', "step 3 thread_id"),
    ("run-step3.log", r'"question_id": "[0-9a-f]{32}"', "step 3 question_id"),
    ("run-step3.log", r'"expires_at": "[^"]+"', "step 3 expires_at"),
    ("run-deployed.log", r"question arrived after [\d.]+s", "deployed: arrival"),
    ("run-deployed.log", r"open questions in the approvals table \(GSI by_status\): \d+", "deployed: open questions"),
    ("run-deployed.log", r"refunds recorded [^\n:]+: \d+", "deployed: refund count"),
    ("run-deployed.log", r"rows for this thread still status=open after the refund: \d+", "deployed: open rows"),
    ("run-deployed.log", r"further questions published for this thread: \d+", "deployed: republished"),
    ("run-deployed-timeout.log", r"question arrived after [\d.]+s", "deadline: arrival"),
    ("run-deployed-timeout.log", r"expires_at  : [\dTZ:-]+", "deadline: expires_at"),
    ("run-deployed-timeout.log", r'"Name": "refund-timeout-[0-9a-f]+"', "deadline: schedule name"),
    ("run-deployed-timeout.log", r'"ScheduleExpression": "at\([^"]+\)"', "deadline: at() expression"),
    ("run-deployed-timeout.log", r"waiting \d+s for the deadline", "deadline: wait"),
    # 2026-09-28: was `the agent resumed at ..., Ns after expires_at`. The owner cut every
    # reference to the 22 s delivery gap, because Scheduler's latency is not what the post
    # argues. Re-pointed at a line the post still quotes rather than deleted, so this block
    # keeps a log rule over it.
    ("run-deployed-timeout.log", r"the schedule fired and deleted itself \(ActionAfterCompletion=DELETE\)", "deadline: fired and self-deleted"),
    ("run-deployed-timeout.log", r"refunds recorded [^\n:]+: \d+", "deadline: refund count"),
    # 2026-09-27: the schedule-DLQ line and the pytest summary are no longer quoted by the post
    # (the build-diary aside and "Reproduce it" were cut); the logs stay as README evidence.
]
for logname, rx, label in LOG_RULES:
    logpath = folder / logname
    if not logpath.exists():
        print(f"SKIP {logname:18} {label:24} (log not present)")
        continue
    found = sorted(set(m.group(0) for m in re.finditer(rx, logpath.read_text(encoding="utf-8", errors="replace"))))
    if not found:
        print(f"SKIP {logname:18} {label:24} (pattern not in log)")
        continue
    for val in found:
        checked += 1
        ok = val in blocks
        failed += not ok
        print(f"{'OK  ' if ok else 'DIFF'} {logname:18} {label:24} log={val!r:44} {'in a block' if ok else 'NOT in any block'}")

# 2026-09-27: the last-four-REPORT-lines rule was removed with the benchmark section; the post no
# longer quotes REPORT lines. run-deployed-lambda.log stays as README evidence.

# Pinned version: every "agent-wait X.Y.Z" in the post (prose and captions) and in every SVG
# must equal the version actually installed, read from uv.lock, never typed here. Narrow on
# purpose: the other packages in the post legitimately carry different numbers.
# (2026-09-27: a figure still said 0.7.0 after the post moved to 0.8.0; no rule covered artwork.)
lock = folder / "uv.lock"
pinned = None
if lock.exists():
    m = re.search(r'name = "agent-wait"\nversion = "([^"]+)"', lock.read_text(encoding="utf-8"))
    pinned = m.group(1) if m else None
if not pinned:
    print("SKIP uv.lock            agent-wait version       (not resolvable from uv.lock)")
else:
    checked += 1
    version_rx = re.compile(r"agent[-_]wait`?\s+(\d+\.\d+\.\d+)")
    hits = [("blog.md", v) for v in version_rx.findall(blog)]
    hits += [(f"images/{name}.svg", v) for name, body in sorted(svgs.items()) for v in version_rx.findall(body)]
    wrong = [(where, v) for where, v in hits if v != pinned]
    for where, v in wrong:
        print(f"DIFF {where:18} {'agent-wait version':24} says {v!r}, uv.lock has {pinned!r}")
    failed += bool(wrong)
    if not wrong:
        print(f"OK   {'uv.lock':18} {'agent-wait version':24} {pinned!r} in {len(hits)} mention(s), all equal")

# Caption = artwork (2026-09-27). Three times a caption stayed green while describing a figure
# that had been redrawn (the 496, the 0.7.0, the old topology), because agreement between the
# caption and the drawing rested on somebody remembering to swap it. The aria-label travels with
# the SVG, so it is the single source: the alt text in blog.md must equal it, whitespace aside.
# No fuzzy matching. A figure with no aria-label is a SKIP that names the figure, not a pass.
def _ws(s: str) -> str:
    return " ".join(s.split())


for name, body in sorted(svgs.items()):
    m = re.search(r'aria-label="([^"]*)"', body)
    if not m:
        print(f"SKIP {name:18} {'caption = aria-label':24} (no aria-label on the SVG)")
        continue
    if name not in alts:
        print(f"SKIP {name:18} {'caption = aria-label':24} (no image line in blog.md)")
        continue
    aria, alt = _ws(html.unescape(m.group(1))), _ws(alts[name])
    checked += 1
    if aria == alt:
        print(f"OK   {name:18} {'caption = aria-label':24} {len(alt)} chars, identical")
    else:
        failed += 1
        i = next((k for k, (a, b) in enumerate(zip(aria, alt)) if a != b), min(len(aria), len(alt)))
        print(f"DIFF {name:18} {'caption = aria-label':24} diverge at char {i}: "
              f"caption '…{alt[max(0, i - 20):i + 30]}…' vs aria-label '…{aria[max(0, i - 20):i + 30]}…'")

print(f"\n{checked} checked, {failed} differences")
sys.exit(1 if failed else 0)
