# Externalize agent interrupts for HITL

*Human-in-the-loop for LangGraph on AWS Lambda — the agent stops for a human, the process dies, and the run continues later with no server and no state in your code.*

Every human-in-the-loop tutorial I have read assumes a process that is still running when the human answers. The graph calls `interrupt()`, the notebook prints the question, you type an answer, the run continues. That assumption is doing all the work, and it is the first thing to go in production.

A refund over the limit needs finance. Finance takes days. Lambda gives you fifteen minutes.

This post is four runnable acts, in order of what breaks. Two packages do the work, each doing one thing, and neither one imports the other: `langgraph-dynamodb-checkpoint` 0.5.0 makes the parked thread a row, and `agent-wait` 0.7.0 makes the interrupt a message. Everything quoted below is pasted from a real run; the scripts, the logs and the tests are at [github.com/skamalj/agentstate-examples/tree/main/approval-on-lambda](https://github.com/skamalj/agentstate-examples/tree/main/approval-on-lambda).

![End to end: a start message on answers.fifo triggers invocation 1; the agent calls escalate_refund, @wait parks it, the run ends with __interrupt__. DynamoDBSaver writes the parked thread (8 items) and publish_interrupts puts the envelope on questions.fifo and an open row in the approvals table. Then nothing runs for days. Finance reads the envelope, copies reply_with, sets the answer and sends it to answers.fifo; invocation 2 loads the thread back from DynamoDB, resumes, and issues the refund once.](images/whole-flow.png)

That is the finished shape, and the gap in the middle of it — where nothing runs for days — is the whole point. The four acts below build it one failure at a time.

The agent is a LangChain `create_agent` over Claude Sonnet 4.6, with two tools and a limit of ₹25,000. `issue_refund` is ordinary. `escalate_refund` is the same body behind one decorator:

```python
@tool
@wait(FINANCE)
def escalate_refund(order_id: str, amount: int) -> str:
    """Refund an order of more than 25000 rupees. Finance approves before any money moves."""
    return "finance approved; " + _refund(order_id, amount)
```

Two tools rather than one tool with an `if`, because `@wait` is not conditional — it parks *every* call to the function it decorates. Which tool to call is the model's decision, off the tool descriptions and the limit in the system prompt.

Which raises the obvious question, and it is the first one an approvals person asks: what stops the model calling `issue_refund` for ₹41,000? Nothing, if the limit only lives in the prompt. So it does not:

```python
@tool
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order of 25000 rupees or less. Runs immediately."""
    if amount > APPROVAL_LIMIT:
        return f"refused: {amount} is over the {APPROVAL_LIMIT} approval limit; call escalate_refund"
    return _refund(order_id, amount)
```

The model picks the tool; the tool enforces the limit. A model that picks wrong gets the refusal back as a tool result and calls `escalate_refund` instead — a routing mistake, self-correcting, costing one turn. A model that picks wrong against a prompt-only limit moves ₹41,000 without asking anyone, which is not a mistake, it is an incident.

That is the same distinction as the two clocks further down, one layer up: a published intention is not an enforced one. Every rule you would be unhappy to see broken has to live somewhere you control — the tool body, or a router node in front of it. The prompt is a hint about which door to knock on, not the lock.

`FINANCE` is what the asker declares about the question. Every field of it gets published and none of it is enforced; that turns out to matter a great deal, and there is a section on it below.

```python
TIMEOUT = os.environ.get("REFUND_WAIT_TIMEOUT", "P3D")

FINANCE = WaitPolicy(
    timeout=TIMEOUT,
    default={"action": "reject", "reason": f"no finance response within {TIMEOUT}"},
    allowed_actions=("approve", "reject"),
    tags={"approver_group": "finance"},
)
```

## Act 1: it vanishes

`InMemorySaver`, a customer asking for ₹41,000 back, and a process that then exits — which on Lambda happens within minutes of the last invocation, and in a container happens on the next deploy.

```text
run 1: parked on 1 question(s)
  question_id=3b6bc3d4a85dd6eae5c25a01c4c45191
  value={'question': {'function': 'escalate_refund', 'args': {'order_id': 'order-4471', 'amount': 41000}}, ...}
run 1: refunds issued = []

--- interpreter exits here ---

run 2: messages on the thread: 1
run 2: agent says: Hello! I'm the refunds agent for our online store. How can I help you today?
run 2: refunds issued = []
```

Run 2 is a fresh interpreter, the same `thread_id`, and finance's approval passed in as `Command(resume={question_id: {"action": "approve"}})`. It does not raise. It does not say "I don't know that thread". It greets the customer, because to LangGraph this is a brand-new conversation with an empty checkpoint and a resume value nobody asked for.

The refund never happens, the customer is never told, and nothing anywhere logs an error. That is the failure mode you are actually designing against.

There is a wall in this system, and act 1 fails because everything it needs is on the wrong side of it. The picture below is that wall: the graph run, the interrupt value, the saver and the side-effect list all live in a process that is about to end; the tables and the queues outlive it. The next two acts are about carrying two things across — the thread, then the question.

![A wall between what lives in the process and dies with it (the graph run, the interrupt value, InMemorySaver, the REFUNDS_ISSUED list) and what lives in AWS and survives (checkpoint table, questions.fifo, approvals table, side-effect counter). DynamoDBSaver carries the thread across in act 2; publish_interrupts carries the question across in act 3. Act 1 fails on the left of the wall.](images/process-boundary.png)

## Act 2: the parked thread survives

One line changes.

```python
from langgraph_dynamodb_checkpoint import DynamoDBSaver

saver = DynamoDBSaver(TABLE)          # creates the table if it is not there
agent = refund.build_agent(saver)
```

`langgraph-dynamodb-checkpoint` 0.5.0 creates the table if it is missing — `PAY_PER_REQUEST`, waits until it is active — so this act needs no setup at all. (In a locked-down Lambda role that cannot call `CreateTable`, you pre-create it; the stack in act 4 does.)

The demo runs the graph in child interpreters, so "the process died" is a process exit and not a comment. One child parks. It exits. The parent then queries DynamoDB on `PK = thread_id` and prints what exists at that moment:

```text
  [pid 36816] parked on 1 question(s)
  [pid 36816] question    = {'function': 'escalate_refund', 'args': {'order_id': 'order-4471', 'amount': 41000}}
  [pid 36816] refunds issued this process: []

--- interpreter 1 is gone. What is in DynamoDB right now ---

Query on PK='order-4471-a2d5f9f5': 8 items
  checkpoint SK='1f1b8445-fd5b-612b-bfff-132d24fb0073'   type='msgpack', 384 bytes
  checkpoint SK='1f1b8445-fd6e-68ce-8000-4f45a6520f3d'   type='msgpack', 700 bytes
  checkpoint SK='1f1b8446-0bf9-691b-8001-4ad87d4d2ba8'   type='msgpack', 2172 bytes
  writes     SK='...-fd5b-...$50ff9cca-...$0'            channel='messages', 116 bytes
  writes     SK='...-fd5b-...$50ff9cca-...$1'            channel='branch:to:model', 0 bytes
  writes     SK='...-fd6e-...$41ebf892-...$0'            channel='messages', 1240 bytes
  writes     SK='...-fd6e-...$41ebf892-...$1'            channel='__pregel_tasks', 188 bytes
  writes     SK='...-0bf9-...$4751462b-...$-3'           channel='__interrupt__', 496 bytes
```

Three checkpoints, one per super-step, and five pending writes hanging off them. The tree below draws the parentage those sort keys encode; it is one table, and there is no GSI anywhere in it.

The pending writes are the part that matters. The last leaf on that tree, `channel='__interrupt__'`, 496 bytes, *is* the parked question. A checkpointer that only wrote checkpoints would lose it, and the resume from a cold process would re-run the node from a state that does not know it ever asked. The package loads them back into `CheckpointTuple.pending_writes`, which is what makes cross-process interrupt and resume work at all; its author reports it passing LangGraph's conformance suite at `FULL` against a live table. Every read in the resume path is a `ConsistentRead`.

![The parked thread as eight DynamoDB items under PK order-4471-a2d5f9f5: three checkpoints, one per super-step (384, 700, 2172 bytes), each with its pending writes as child items keyed <checkpoint_id>$<task_id>$<index>. The last write, channel __interrupt__, 496 bytes, is the parked question. On resume the five writes are loaded into CheckpointTuple.pending_writes.](images/parked-thread.png)

Then a second child interpreter — new pid, new everything — resumes the same thread:

```text
--- interpreter 2: finance answers, three days later ---
  [pid 11804] refunds issued this process: ['order-4471']
  [pid 11804] agent says: Great news — Finance has approved and processed a refund of
              ₹41,000 for order order-4471.

items after the resume: 14 (was 8)
```

The refund ran in a process that did not exist when the question was asked. Setup and IAM for the checkpointer are at [skamalj.github.io/agentstate-reducer/langgraph/dynamodb](https://skamalj.github.io/agentstate-reducer/langgraph/dynamodb/); I will not restate them here.

## Act 3: but the question is trapped

Act 2 fixed durability and nothing else. The checkpoint knows the graph is waiting. Nobody else does — not the finance team, not an approvals dashboard, not a Slack channel. There is no event, no row, no message. Somebody has to go and ask LangGraph.

The `@wait` decorator has been on the tool since act 1, and on its own it does exactly one thing: it shapes the interrupt value into `{"function": ..., "args": {...}}` and attaches the policy to it. It does not tell anybody. That is the other half of [`agent-wait` 0.7.0](https://skamalj.github.io/agent-wait/), one call after the run:

```python
announce = [SqsAnnounce(os.environ["REFUND_QUESTIONS_QUEUE"]), DynamoDbAnnounce(APPROVALS)]
...
result = agent.invoke(value, config)
publish_interrupts(result, thread_id, announce)
```

`publish_interrupts` reads `result["__interrupt__"]` and nothing else. No graph handle, no `get_state()`, no checkpointer — it never touches the thing act 2 built. If nothing interrupted, it does nothing.

Here is the whole envelope that landed on the queue:

```json
{
  "type": "wait.created",
  "event_id": "01M3AA94A3CG2QR8V8X7HV5E10",
  "thread_id": "order-4471-0db3659a",
  "question_id": "2a54b5108aeffb7eea7d66df74ca16d8",
  "question": {
    "function": "escalate_refund",
    "args": { "order_id": "order-4471", "amount": 41000 }
  },
  "allowed_actions": ["approve", "reject"],
  "expires_at": "2026-09-27T18:19:10Z",
  "default": { "action": "reject", "reason": "no finance response within P3D" },
  "answer_ttl": null,
  "source": { "function": "escalate_refund" },
  "reply_to": null,
  "reply_with": {
    "thread_id": "order-4471-0db3659a",
    "question_id": "2a54b5108aeffb7eea7d66df74ca16d8",
    "answer": null
  },
  "correlation": null,
  "tags": { "approver_group": "finance" }
}
```

`question_id` is LangGraph's own `Interrupt.id`, which is why a republished question keeps its identity. `reply_with` is a filled-in stub: copy it, set `answer`, send it back — the only empty field in the whole document, as the grouping below makes plain.

![The wait.created envelope in four groups: identity (event_id, thread_id, question_id = Interrupt.id), the question shaped by @wait, the policy fields that are published but not enforced (allowed_actions, expires_at, default, answer_ttl, tags), and reply_with, a pre-filled reply whose only empty field is answer. Finance copies it, sets the answer and sends it to answers.fifo, where the host's one if turns it into Command(resume). A timeout is the same shape: send envelope.default as the answer.](images/envelope.png)

The same call also wrote the question as a row, because `DynamoDbAnnounce` was in the list — `pk = THREAD#<thread>`, `sk = WAIT#<question_id>`, `status = "open"`, and a `by_status` GSI so a dashboard can query "everything still open, oldest deadline first" without building a projection off a queue. One call, two places: the queue that wakes someone, and the row an operator reads.

The return leg is one `if`, and it is the only integration code in the whole demo:

```python
if "question_id" in message:  # an answer
    value = Command(resume={str(message["question_id"]): message.get("answer")})
else:  # a start
    value = message.get("input")
```

Finance approves. A new interpreter picks the answer off the answers queue and resumes:

```text
--- host run 2: the answer arrives, in a new interpreter ---
  [pid 32708] refunds issued this process: ['order-4471']
```

Then the interesting part. The same answer is delivered a second time, and then a *different* answer arrives late:

```text
--- the same answer arrives a second time ---
  [pid 29200] refunds issued this process: []

--- and a different answer, after the fact ---
  [pid 22404] refunds issued this process: []

refunds recorded in DynamoDB for order-4471, across all four runs: 1
```

Nothing ran either time. A `Command(resume=...)` for a question the thread has already moved past is a no-op in LangGraph, so the host needs no ledger, no idempotency key and no check. The first decision stands. The counter is an unconditional `ADD calls :one` in DynamoDB rather than a Python list, precisely so "it ran once" is a fact outside any process.

## Two clocks, and only one of them is real

This is the part nobody writes down, and it is where a system like this silently loses a refund.

Both packages have a notion of time, and they mean completely different things by it.

`WaitPolicy(timeout="P3D")` publishes `expires_at` on the envelope. **Nothing enforces it.** agent-wait never sees an answer, so it cannot act on a deadline. What is supposed to happen is that whoever consumes the envelope notices the deadline passed and sends the policy's own `default` back as an ordinary answer — the same shape as a human clicking reject. If the consumer sends nothing, the thread waits forever. That is LangGraph, and it is fine.

There is a test for the deadline path, because it is the one people assume is automatic. It takes `envelope["default"]` verbatim and sends it as the answer:

```python
out = answer(local, envelope, envelope["default"])   # {"action": "reject", "reason": "no finance response within P3D"}
assert "refunds issued this process: []" in out
assert calls(local, order_id) == 0
```

A timeout is a rejection somebody sent. There is no other kind.

`DynamoDBSaver(ttl_seconds=...)` stamps a `ttl` attribute on every item it writes, and the package enables the table's TTL on that attribute. **AWS enforces this one.** DynamoDB deletes the parked thread whether or not anyone is still holding a question about it.

So set the advisory one to three days and the enforced one to one day, and run it:

```text
WaitPolicy(timeout='P3D')
DynamoDBSaver(ttl_seconds=86400)

parked items: 8, every one with a ttl attribute
{
  "table TTL": { "TimeToLiveStatus": "ENABLED", "AttributeName": "ttl" },
  "checkpoint ttl (epoch)": 1790360377,
  "checkpoint ttl (utc)": "2026-09-25T18:19:37Z",
  "envelope expires_at": "2026-09-27T18:19:38Z"
}

the question outlives the thread by 2 days, 0:00:01
```

The question in the approvals table says finance has until the 27th. The thread it would resume is deleted on the 25th. On day two, the approval arrives against a thread that no longer exists:

```text
--- DynamoDB reaches the ttl and deletes the thread ---
items for this thread: 0

--- day two: finance approves the question it can still see ---
  [pid 37200] refunds issued this process: []
  [pid 37200] agent says: Hello! Welcome to our refunds support. How can I help you today?

refunds recorded in DynamoDB for order-4471: 0
```

That is act 1 again, arrived at from the other direction — and this time the operator is looking at an open question in a dashboard that promises three days. The demo deletes the items itself rather than waiting a day, because DynamoDB's sweeper runs within 48 hours of expiry and no blog post waits that long. The effect is identical: an approved refund that never happens, with no error anywhere.

Put both deadlines on one axis and the bug is a shape rather than an argument. The shaded band below is the two days in which the question is still answerable and the thread is already gone. Nothing in either package can see that band: one of them wrote the left edge, the other published the right, and neither knows the other exists.

![A timeline from publish on Sep 24 18:19. The enforced clock, DynamoDB ttl (ttl_seconds=86400), deletes the parked thread at 2026-09-25T18:19:37Z. The advisory clock, expires_at (timeout P3D), tells finance the question is open until 2026-09-27T18:19:38Z. The two-day gap is shaded: an approval landing there resumes a thread that no longer exists, the agent greets the customer, refunds recorded: 0.](images/two-clocks.png)

Which of the two is lying to you? `expires_at` is. It is a published intention, measured from publish time, that nothing in the system is obliged to honour. `ttl` is a fact. If you set both, the checkpoint TTL is the real deadline for the whole workflow, and every `expires_at` you publish has to fit inside it. The safe default is not to set `ttl_seconds` at all on a table holding threads that are parked on humans.

## Act 4: deployed

Same `graph.py`, same host `if`, on Lambda. Two FIFO queues, one function, three tables — the third being the refund counter, which exists only so the demo can prove what happened. The topology below is the whole of it, and the thing worth noticing is what is *not* in the picture: no scheduler, no wait store, no sweeper. Neither package keeps state of its own, so there is nothing else to run, and between the question going out and the answer coming back there is no process at all.

![Deployed topology: a laptop script puts starts and answers on answers.fifo (MessageGroupId = thread_id, batch size 1, DLQ after 3 receives), which triggers one Lambda (handler.py + graph.py, Python 3.12, 1 GB, 120 s) calling Bedrock Claude Sonnet 4.6. The function writes the checkpoints table (PK/SK, ttl attribute, no GSI), the approvals table (GSI by_status) and the side-effects counter, and SqsAnnounce publishes the envelope to questions.fifo (dedupe id = dedupe_key), which finance reads.](images/deployed-topology.png)

A script on my laptop plays the customer and then finance. It never imports the agent; it only puts JSON on a queue and reads JSON off another one.

```text
--- the customer's message is on the queue; nothing of ours is running ---
question arrived after 7.1s
```

That 7.1 seconds is SQS delivery plus a cold start plus a model call. Here is what the function logged for the whole approval, with request ids stripped:

```text
agent-wait {"event": "wait.created", "thread_id": "order-fc56ea", "question_id": "69c801fd266478c44601a51789d2417b",
            "allowed_actions": ["approve", "reject"], "expires_at": "2026-09-27T18:32:11Z",
            "tags": {"approver_group": "finance"}}
{"thread_id": "order-fc56ea", "kind": "start",  "parked_on": ["69c801fd..."], "refunds_by_this_container": [], "reply": ""}
REPORT Duration: 1642.21 ms  Billed Duration: 5718 ms  Max Memory Used: 178 MB  Init Duration: 4075.36 ms

{"thread_id": "order-fc56ea", "kind": "answer", "parked_on": [], "refunds_by_this_container": ["order-fc56ea"],
 "reply": "Finance has reviewed and approved your refund of ₹41,000 for order order-fc56ea — the money
           will be returned to your original payment method shortly."}
REPORT Duration: 1697.55 ms  Billed Duration: 1698 ms  Max Memory Used: 179 MB

{"thread_id": "order-fc56ea", "kind": "answer", "parked_on": [], "refunds_by_this_container": ["order-fc56ea"], ...}
REPORT Duration: 15.31 ms  Billed Duration: 16 ms  Max Memory Used: 179 MB

{"thread_id": "order-fc56ea", "kind": "answer", "parked_on": [], "refunds_by_this_container": ["order-fc56ea"], ...}
REPORT Duration: 14.45 ms  Billed Duration: 15 ms  Max Memory Used: 179 MB
```

Four invocations, and on one scale below they are two tall bars and two slivers. The first is the customer's message and pays a 4.1-second cold start on top of a 1.6-second run. The second is finance's approval: 1.7 seconds, including the model call that writes the reply. The third and fourth are the duplicate and the late rejection: **15 milliseconds each**. That is what a no-op resume costs — load the thread from DynamoDB, see the question has been answered, return. No model call, because nothing ran, which is why the last two bars barely exist. Only the durations are to scale; in wall-clock time those four bars are days apart.

![The four Lambda invocations on one millisecond scale: 1 start = 4075.36 ms cold start + 1642.21 ms run (billed 5718); 2 answer = 1697.55 ms; 3 duplicate answer = 15.31 ms; 4 late rejection = 14.45 ms, no model call. Invocations are days apart; only durations are to scale.](images/four-invocations.png)

`refunds_by_this_container` says `["order-fc56ea"]` on all three answers, and that is not a bug in the demo. It is a module-level Python list, and Lambda reused the container, so it holds everything that container has ever refunded. The number that means something is the counter in DynamoDB, which is why the counter is in DynamoDB:

```text
open questions in the approvals table (GSI by_status): 1
refunds recorded so far: 0

--- finance approves, three days later as far as anything here knows ---
refunds recorded after the approval: 1
rows for this thread still status=open after the refund: 1

--- the same answer twice more ---
refunds recorded after three answers in total: 1
further questions published for this thread: 0
```

Read the third line again. The refund has been approved, the money has moved, and the row in the approvals table still says `status = "open"`. `DynamoDbAnnounce` writes that row once and never touches it again, on purpose — it is not on the receive path and cannot know the question was answered. Point a dashboard straight at that table and it will show finance a question they already approved, with a live-looking Approve button on it. Closing the row is the host's job; this host does not do it, and nothing warned me. Worth knowing before that table becomes a queue somebody works from.

The whole approval billed 7.4 GB-seconds of Lambda, four requests, a handful of DynamoDB writes and eight SQS messages. Between the question going out and the answer coming back, that cost is zero: there is no process, no polling loop and no scheduled sweep. The thread is a few rows and the question is a message. Everything was torn down with `cdk destroy` when the run finished.

**Why FIFO on both legs.** Outbound, `SqsAnnounce` sets `MessageDeduplicationId` to the envelope's `dedupe_key` (`wait.created:<question_id>`, stable across republishes), so a redelivered start message that re-runs the thread and republishes the same question is swallowed by the queue itself. Inbound, `MessageGroupId = thread_id` means two answers for one thread are never processed concurrently. On a standard queue, two Lambdas could pick up two answers for the same thread and both try to resume the same checkpoint. Neither of those is a correctness requirement — LangGraph would ignore the second resume anyway — but one is a duplicate approval in someone's inbox and the other is a race against DynamoDB, and both are free to avoid.

The FIFO deduplication window is five minutes, not a ledger. For anything longer, the envelope's `dedupe_key` is what a consumer deduplicates on.

This stack passes three announcers — a log line, the queue, the row — and that list is where fan-out lives. `SnsAnnounce` turns the policy's `tags` into message attributes, so a subscription filter routes `approver_group = finance` to one queue and everything else to another without the agent knowing either exists. `EventBridgeAnnounce` puts it on a bus where rules split one question across Slack, a ticket and an audit log. `WebhookAnnounce` is a signed POST, in the stdlib, no extra. Anywhere else — Kinesis, Kafka, a Slack client, a Redis key — is a `BaseAnnounce` subclass with one method; those are the dimmed boxes below, and the distance between them and the shipped ones is that one method. Still one `publish_interrupts` call.

![One publish_interrupts(result, thread_id, announce) call fans out to a list. Ships in agent-wait 0.7.0: SqsAnnounce, DynamoDbAnnounce and LogAnnounce (filled, used in act 4), plus SnsAnnounce, EventBridgeAnnounce and WebhookAnnounce. Separated and dimmed, not shipped: Kinesis, Kafka, Slack, a Redis key, a Postgres row, each one BaseAnnounce subclass with a deliver(envelope, transition) method away.](images/announcer-fanout.png)

## What each package will not do for you

`langgraph-dynamodb-checkpoint`: one item holds the whole serialized checkpoint, so DynamoDB's 400 KB item limit is the ceiling on your state — and on an unbounded message list. Every local run log in this project carries the package's own INFO line about that, once per process; the Lambda sets `AGENTSTATE_QUIET=1`. `delete_for_runs` is a filtered `Scan` and only finds items written by 0.5.0 or later. `prune(strategy="keep_latest")` is not `DeltaChannel`-aware. The async API is executor-backed rather than native `aioboto3`.

`agent-wait`: it never receives an answer, so `expires_at`, `default`, `answer_ttl` and `allowed_actions` are all published and none of them are enforced. `DynamoDbAnnounce` is an unconditional `put_item`, so a host that treats the row as a ledger — open, answered, closed — has to read it and skip republishing itself; the adapter will not do that read, on purpose. And on langgraph 1.2.x there is one `interrupt()` per node, so each waiting tool needs its own node.

## What's next

The 400 KB ceiling is a real one for a long-lived thread. The same checkpointer takes a `reducer=` that bounds the message list inside the save, and an `on_prune` hook that hands the messages leaving the window to long-term memory instead of dropping them — which is a different post, and is written up at [skamalj.github.io/agentstate-reducer/reducer/long-term-memory](https://skamalj.github.io/agentstate-reducer/reducer/long-term-memory/).

## Reproduce it

```bash
git clone https://github.com/skamalj/agentstate-examples
cd agentstate-examples/approval-on-lambda
uv venv -p 3.12 && uv sync --all-groups
export AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=ap-south-1
uv run python act1_vanishes.py
uv run python act2_survives.py
uv run python act3_asks.py
uv run python two_clocks.py
uv run pytest -q
```

Act 4 is a `uv pip install --target` for the Lambda runtime and a `cdk deploy`; the README has the exact commands, the IAM the role ends up with, and the `cdk destroy` that removes it. No docker anywhere, including for the bundle.

The model is real Bedrock. DynamoDB and SQS are not: acts 2, 3 and the two-clocks run point `AWS_ENDPOINT_URL_DYNAMODB` and `AWS_ENDPOINT_URL_SQS` at a local `moto_server`, so nothing is created in your account and there is nothing to clean up. Set those variables at DynamoDB Local, or empty them for the real services, and the same scripts run there.

Ten tests, all of them about the claims above: the run parks instead of refunding, a *different* process resumes it, the refund runs exactly once across three answers, a duplicate answer runs nothing, the envelope carries the documented fields, the question is also a row, the policy's `default` is an ordinary answer, an under-limit refund asks nobody, `ttl_seconds` stamps every parked item, and the approval limit is enforced by the tool rather than by the prompt.

```text
..........                                                               [100%]
10 passed in 68.31s (0:01:08)
```

## The point

The interrupt is the easy part — LangGraph ships it. What makes it useless on serverless is that the framework stops exactly there: the question exists only inside a process that is about to die, and nothing else in your system can see it or answer it.

Two problems, two packages, neither one aware of the other. Make the parked thread a row, and the process may die. Make the interrupt a message, and someone outside the process can answer it. Then the agent waits for three days without waiting for anything, because nothing is running.
