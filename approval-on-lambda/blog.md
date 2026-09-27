# Externalize agent interrupts for HITL

*Human-in-the-loop for LangGraph on AWS Lambda. The agent stops for a human, the process dies, and the run continues later, with no server and no state in your code.*

Human-in-the-loop tutorials assume the process is still running when the human answers. The graph calls `interrupt()`, you type an answer, the run continues. Production breaks that assumption first.

A refund over the limit needs finance. Finance takes days. Lambda gives you fifteen minutes. An event-driven runtime dies as soon as the event is processed. Even AgentCore gives a session's compute eight hours, and when it goes, anything held in memory goes with it.

Two packages do the work. [`langgraph-dynamodb-checkpoint`](https://skamalj.github.io/agentstate-reducer/langgraph/dynamodb/) 0.5.0 makes the parked thread a row. [`agent-wait`](https://skamalj.github.io/agent-wait/) 0.8.0 makes the interrupt a message. Every number and log line below comes from a real run. The code, logs and tests are at [github.com/skamalj/agentstate-examples/tree/main/approval-on-lambda](https://github.com/skamalj/agentstate-examples/tree/main/approval-on-lambda).

![End to end: a start message on answers.fifo triggers invocation 1; the agent calls issue_refund, @wait parks it, the run ends with __interrupt__. DynamoDBSaver writes the parked thread (8 items) and publish_interrupts puts the envelope on questions.fifo and an open row in the approvals table. Then nothing runs for days. Finance reads the envelope, copies reply_with, sets the answer and sends it to answers.fifo; invocation 2 loads the thread back from DynamoDB, resumes, and issues the refund once.](images/whole-flow.png)

Nothing runs during the gap in the middle. That gap is the problem.

The agent is a LangChain `create_agent` over Claude Sonnet 4.6. One tool, and a limit of ₹25,000. The limit is a predicate on the decorator:

```python
@tool
@wait(FINANCE, when=lambda order_id, amount: amount > APPROVAL_LIMIT)
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order. Refunds over 25000 rupees need finance to approve first."""
    REFUNDS_ISSUED.append(order_id)
    return f"refunded {amount} for {order_id}"
```

Under the limit the body runs and nobody is asked. Over it, the call parks. The predicate sees the published `args`, runs again on resume so it must be deterministic, and parks the call if it raises.

A rule in a prompt is a request. A rule in code is a rule. Anything you would be upset to see broken belongs in code.

`FINANCE` is what the asker declares. Every field is published, none is enforced:

```python
TIMEOUT = os.environ.get("REFUND_WAIT_TIMEOUT", "P3D")

FINANCE = WaitPolicy(
    timeout=TIMEOUT,
    default={"action": "reject", "reason": f"no finance response within {TIMEOUT}"},
    allowed_actions=("approve", "reject"),
    tags={"approver_group": "finance"},
)
```

## Step 1: an unpersisted checkpoint is lost

A checkpoint that is not persisted is lost when the process dies or restarts. `InMemorySaver` holds it in heap memory, so the parked question dies with the interpreter. On Lambda that is minutes after the last invocation; in a container, the next deploy. A customer asks for ₹41,000 back, and the process exits:

```text
run 1: parked on 1 question(s)
  question_id=f20790e1bb90795863382d749ab65172
  value={'question': {'function': 'issue_refund', 'args': {'order_id': 'order-4471', 'amount': 41000}}, ...}
run 1: refunds issued = []

--- interpreter exits here ---

run 2: messages on the thread: 1
run 2: agent says: Hello! How can I help you today?
run 2: refunds issued = []
```

Run 2 is a fresh interpreter, same `thread_id`, with finance's approval passed in as `Command(resume={question_id: {"action": "approve"}})`. It does not raise. It greets the customer: the checkpoint went with the first interpreter, so this is a new conversation with a resume value nobody asked for.

The refund never happens. The customer is never told. Nothing logs an error.

Persisting the checkpoint fixes half of it. The question has to get out of the process too. Steps 2 and 3.

![A wall between what lives in the process and dies with it (the graph run, the interrupt value, InMemorySaver, the REFUNDS_ISSUED list) and what lives in AWS and survives (checkpoint table, questions.fifo, approvals table, side-effect counter). DynamoDBSaver carries the thread across in step 2; publish_interrupts carries the question across in step 3. Step 1 fails on the left of the wall.](images/process-boundary.png)

## Step 2: the parked thread survives

One line changes.

```python
from langgraph_dynamodb_checkpoint import DynamoDBSaver

saver = DynamoDBSaver(TABLE)          # creates the table if it is not there
agent = refund.build_agent(saver)
```

In step 1 the checkpoint was not persisted. Now it is. The demo runs the graph in child interpreters, so the process really exits: one child parks and exits, and the parent then queries DynamoDB:

```text
  [pid 28932] parked on 1 question(s)
  [pid 28932] question_id = 048c1fc9bae482cf1776509f8e0fe530
  [pid 28932] question    = {'function': 'issue_refund', 'args': {'order_id': 'order-4471', 'amount': 41000}}
  [pid 28932] refunds issued this process: []

--- interpreter 1 is gone. What is in DynamoDB right now ---

Query on PK='order-4471-a8aad5a8': 8 items
  checkpoint SK='1f1ba728-2742-61dd-bfff-bb2ad2ad9981'
             type='msgpack', 384 bytes, ttl=-
  checkpoint SK='1f1ba728-2758-633f-8000-354836cf8ecf'
             type='msgpack', 700 bytes, ttl=-
  checkpoint SK='1f1ba728-36f4-6cb6-8001-5a29b68236c1'
             type='msgpack', 2232 bytes, ttl=-
  writes     SK='1f1ba728-2742-61dd-bfff-bb2ad2ad9981$295f47d1-a96a-8c7d-00e1-0db46e932354$0'
             channel='messages', 116 bytes, ttl=-
  writes     SK='1f1ba728-2742-61dd-bfff-bb2ad2ad9981$295f47d1-a96a-8c7d-00e1-0db46e932354$1'
             channel='branch:to:model', 0 bytes, ttl=-
  writes     SK='1f1ba728-2758-633f-8000-354836cf8ecf$cef9638d-3ac8-0f86-4e27-c84053a9d784$0'
             channel='messages', 1304 bytes, ttl=-
  writes     SK='1f1ba728-2758-633f-8000-354836cf8ecf$cef9638d-3ac8-0f86-4e27-c84053a9d784$1'
             channel='__pregel_tasks', 184 bytes, ttl=-
  writes     SK='1f1ba728-36f4-6cb6-8001-5a29b68236c1$4220f82b-d6a7-53b7-12c0-9836f95c294f$-3'
             channel='__interrupt__', 488 bytes, ttl=-
```

Eight items, and the last line is the evidence: `channel='__interrupt__'`, 488 bytes. The parked question is in DynamoDB, not in a dead interpreter's heap.

![The parked thread as eight DynamoDB items under PK order-4471-a8aad5a8: three checkpoints, one per super-step (384, 700, 2232 bytes), each with its pending writes as child items keyed <checkpoint_id>$<task_id>$<index>. The last write, channel __interrupt__, 488 bytes, is the parked question. On resume the five writes are loaded into CheckpointTuple.pending_writes.](images/parked-thread.png)

A second child interpreter, new pid, resumes the same thread:

```text
--- interpreter 2: finance answers, three days later ---
  [pid 30460] refunds issued this process: ['order-4471']
  [pid 30460] agent says: Since the refund amount exceeds ₹25,000, it has been sent to the finance team for approval before being processed to your account.

items after the resume: 14 (was 8)
```

## Step 3: but the question is trapped

Step 2 fixed durability and nothing else. The checkpoint knows the graph is waiting; nothing else does.

`@wait` only shapes the interrupt value and attaches the policy. It tells nobody. Telling is the other half of [`agent-wait` 0.8.0](https://skamalj.github.io/agent-wait/): one call after the run packages the result and sends it wherever `announce` says:

```python
announce = [SqsAnnounce(os.environ["REFUND_QUESTIONS_QUEUE"]), DynamoDbAnnounce(APPROVALS)]
...
result = agent.invoke(value, config)
publish_interrupts(result, thread_id, announce)
```

It reads `result["__interrupt__"]` and nothing else: no graph handle, no `get_state()`, no checkpointer. The envelope it sent:

```json
{
  "type": "wait.created",
  "event_id": "01M3HEX4REKTAXV95YVQQ08BXC",
  "thread_id": "order-4471-ebca9020",
  "question_id": "e03c97a8baf667b7565d85a9ef17a87a",
  "question": {
    "function": "issue_refund",
    "args": {
      "order_id": "order-4471",
      "amount": 41000
    }
  },
  "allowed_actions": [
    "approve",
    "reject"
  ],
  "expires_at": "2026-09-30T12:54:41Z",
  "default": {
    "action": "reject",
    "reason": "no finance response within P3D"
  },
  "answer_ttl": null,
  "source": {
    "function": "issue_refund"
  },
  "reply_to": null,
  "reply_with": {
    "thread_id": "order-4471-ebca9020",
    "question_id": "e03c97a8baf667b7565d85a9ef17a87a",
    "answer": null
  },
  "correlation": null,
  "tags": {
    "approver_group": "finance"
  }
}
```

`reply_with` is a filled-in stub: copy it, set `answer`, send it back. `question_id` is LangGraph's own `Interrupt.id`, so a republished question keeps its identity.

![The envelope field by field, in four groups: identity (event_id, thread_id, question_id = LangGraph's Interrupt.id), the question shaped by @wait (issue_refund, order-4471, 41000), the policy fields that are published but not enforced (allowed_actions, expires_at, default, answer_ttl, tags), and reply_with, a pre-filled reply whose only empty field is answer. Finance copies it, sets the answer and sends it to answers.fifo, where the host's one if turns it into Command(resume). A timeout is the same shape: send envelope.default as the answer.](images/envelope.png)

`announce` had two entries, so the question went to two places: a message on the queue, and a row in DynamoDB. The message notifies whoever answers. The row is what an approvals dashboard lists.

The return leg is one `if`, the only integration code:

```python
if "question_id" in message:  # an answer
    value = Command(resume={str(message["question_id"]): message.get("answer")})
else:  # a start
    value = message.get("input")
```

Finance approves, and a new interpreter picks the answer off the queue:

```text
--- host run 2: the answer arrives, in a new interpreter ---
  [pid 28852] refunds issued this process: ['order-4471']
```

Then the same answer again, and a different answer after that:

```text
--- the same answer arrives a second time ---
  [pid 10640] refunds issued this process: []

--- and a different answer, after the fact ---
  [pid 27664] refunds issued this process: []

refunds recorded in DynamoDB for order-4471, across all four runs: 1
```

Neither ran. A `Command(resume=...)` for a question the thread has moved past is a no-op in LangGraph, so the host needs no ledger and no idempotency key. The first decision stands.

## Step 4: deployed

Same `graph.py`, same host `if`, on Lambda. The approve path below is two FIFO queues, one function and three tables.

Neither package asked for a wait store, a sweeper or a scheduler. They keep no state of their own, so nothing runs on their behalf between the question going out and the answer coming back.

![The approval on the deployed stack, numbered to match Step 4. 1: the customer's message goes on answers.fifo, invocation 1 runs the graph and parks, the thread is written to the checkpoints table as rows, and publish_interrupts puts the envelope on questions.fifo and an open row in the approvals table. 2: nothing of ours runs; the thread is rows in DynamoDB, the question is a message and a row. 3: finance reads the envelope, copies reply_with, sets answer, and sends it to answers.fifo. 4: invocation 2 loads the thread from the checkpoints table, resumes it, and the refund runs, side-effects counter 1. 5: the same answer arrives twice more, invocations 3 and 4 are no-ops, the counter stays 1. Two FIFO queues, one function, three tables; the approvals row stays open after the refund.](images/deployed-topology.png)

The customer and finance are both driven from outside the agent, JSON on one queue and JSON off another:

```text
--- the customer's message is on the queue; nothing of ours is running ---
question arrived after 2.3s
```

The approval, end to end:

1. The customer's message goes on `answers.fifo`. The Lambda runs the graph, the tool parks, and `publish_interrupts` puts the envelope on `questions.fifo` and an open row in the approvals table.
2. Nothing of ours runs. The thread is rows in DynamoDB. The question is a message and a row.
3. Finance copies `reply_with`, sets `answer`, and sends it to `answers.fifo`.
4. A second invocation loads the thread from DynamoDB, resumes it, and the refund runs.
5. The same answer arrives twice more. Neither does anything.

The counters, across all of it:

```text
open questions in the approvals table (GSI by_status): 2
refunds recorded so far: 0

--- finance approves, three days later as far as anything here knows ---
refunds recorded after the approval: 1
rows for this thread still status=open after the refund: 1

--- the same answer twice more ---
refunds recorded after three answers in total: 1
further questions published for this thread: 0
```

Note the row still open after the refund. The money has moved and the row still says `status = "open"`. `DynamoDbAnnounce` writes it once and never touches it again: it is not on the receive path and cannot know the question was answered. That is also why two questions show as open. A dashboard pointed at that table shows finance a question they already approved, with a live Approve button. Closing the row is the host's job.

Between the question going out and the answer coming back the cost is zero.

## The deadline, enforced

`FINANCE` declares `timeout="PT2M"` on the deployed stack. Three days is the real policy; two minutes is short enough to watch.

`agent-wait` writes the deadline onto the envelope and does nothing about it:

```text
question arrived after 6.4s
question_id : 5527dd1507fc72b7f0e617b7cb3c8752
expires_at  : 2026-09-27T13:11:10Z
default     : {"action": "reject", "reason": "no finance response within PT2M"}
```

Two machine-readable fields: when the answer is due, and what to do if it never comes. That envelope goes to every destination in the list — a queue a person reads, a table a dashboard reads, and a queue a scheduler reads:

```python
ANNOUNCE = [
    LogAnnounce(),
    SqsAnnounce(os.environ["REFUND_QUESTIONS_QUEUE"]),   # a person reads this
    SqsAnnounce(os.environ["REFUND_TIMEOUTS_QUEUE"]),    # a scheduler reads this
    DynamoDbAnnounce(os.environ["REFUND_APPROVALS_TABLE"]),
]
```

The scheduler Lambda reads it off that second queue and builds the message the deadline will send:

```python
scheduler.create_schedule(
    Name=f"refund-timeout-{envelope['question_id'][:24]}",
    ScheduleExpression=f"at({expires_at.rstrip('Z')})",
    ActionAfterCompletion="DELETE",
    Target={"Arn": ANSWERS_QUEUE_ARN, "RoleArn": SCHEDULER_ROLE,
            "Input": json.dumps(answer_for(envelope)), ...},
)
```

`answer_for` invents nothing: it copies the envelope's `reply_with` stub and drops `default` into `answer`. The Lambda hands that message to EventBridge Scheduler with a time to send it, and exits. Read back from Scheduler, the schedule holds the whole thing:

```text
{
  "Name": "refund-timeout-5527dd1507fc72b7f0e617b7",
  "ScheduleExpression": "at(2026-09-27T13:11:10)",
  "ScheduleExpressionTimezone": "UTC",
  "ActionAfterCompletion": "DELETE",
  "Target": {
    "Arn": "...:approval-on-lambda-Answers9615456C-EmVgk9VoMNAX.fifo",
    "SqsParameters": {
      "MessageGroupId": "order-93852c"
    },
    "Input": {
      "thread_id": "order-93852c",
      "question_id": "5527dd1507fc72b7f0e617b7cb3c8752",
      "answer": {
        "action": "reject",
        "reason": "no finance response within PT2M"
      }
    }
  }
}
```

`Target.Input` is the rejection, written out in full, with nothing running. Its `answer` is the `default` line above it, copied. What the deadline sends is what a human sends by clicking reject.

The name comes from `question_id`, so arming the timer twice raises `ConflictException` instead of setting a second one. `DELETE` means the schedule reaps itself.

At `expires_at`, Scheduler delivers that message to the answers queue, where it is one more answer:

```text
refunds recorded while it waits: 0
open rows for this thread       : 1

--- waiting 216s for the deadline; no process of ours is running ---
the schedule fired and deleted itself (ActionAfterCompletion=DELETE)

refunds recorded after the deadline: 0
```

The reject travelled the graph like any other answer, so the customer got a sentence rather than silence. Then the human turns up late and approves:

```text
--- a human approves, too late ---
refunds recorded after the late approval: 0
```

Nothing runs, and the timeout needed no guard to stop it. It is the same no-op as a duplicate approval: the thread has moved past the question.

![The deadline end to end on thread order-93852c: the start invocation parks and publishes the same envelope to questions.fifo and timeouts.fifo. The scheduler Lambda creates a one-shot schedule named refund-timeout-5527dd1507fc72b7f0e617b7 with at(2026-09-27T13:11:10), ActionAfterCompletion DELETE, targeting answers.fifo with MessageGroupId order-93852c and the envelope's default as the answer. For 216 s no process runs. At expires_at 2026-09-27T13:11:10Z the schedule delivers the default and deletes itself, the agent resumes and rejects, refunds recorded 0. A late human approval is a no-op, refunds still 0.](images/timeout-sequence.png)

The schedule is a row in AWS: no container, nothing billed while it waits. The scheduler Lambda ran once, at question time.

Every queue here is FIFO. Outbound, `SqsAnnounce` sends the envelope's `dedupe_key` as the `MessageDeduplicationId`, so a republished question is swallowed by the queue. Inbound, `MessageGroupId = thread_id` serialises answers for one thread: a human clicking approve and a schedule firing can arrive at once, and on a standard queue that is two Lambdas resuming the same checkpoint. The deduplication window is five minutes, not a ledger.

The announcer list is where fan-out lives, and two of those four entries are consumers with nothing in common. The rest is below. Anywhere it does not cover is a `BaseAnnounce` subclass with one method, and still one `publish_interrupts` call.

![One publish_interrupts(result, thread_id, announce) call fans out to a list. Ships in agent-wait 0.8.0: SqsAnnounce, DynamoDbAnnounce and LogAnnounce (filled, used in step 4), plus SnsAnnounce, EventBridgeAnnounce and WebhookAnnounce. Separated and dimmed, not shipped: Kinesis, Kafka, Slack, a Redis key, a Postgres row, each one BaseAnnounce subclass with a deliver(envelope, transition) method away.](images/announcer-fanout.png)

## The point

LangGraph ships the interrupt and stops there. The question exists only inside a process that is about to die, and nothing else can see it or answer it.

Two problems, two packages. Make the parked thread a row and the process may die. Make the interrupt a message and someone outside can answer it. Then the agent waits three days without waiting for anything, because nothing is running.
