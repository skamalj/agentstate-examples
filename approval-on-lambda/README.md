# approval-on-lambda

Code for the post *Externalize agent interrupts for HITL* (`blog.md` here) — human-in-the-loop
for LangGraph on AWS Lambda.

A refund agent parks on a question for finance, the process dies, and the run continues
days later in a different process — with no server and no state in the application code.
Two packages do it, each doing one thing and neither knowing about the other:

| piece | package |
|---|---|
| the parked thread survives; the checkpoint is a row | `langgraph-dynamodb-checkpoint` 0.5.0 (`DynamoDBSaver`) |
| the question gets out and the answer gets back in | `agent-wait[langgraph,aws]` 0.7.0 (`@wait`, `publish_interrupts`, `SqsAnnounce`, `DynamoDbAnnounce`) |
| the agent | LangChain `create_agent` on LangGraph 1.2.12, Claude Sonnet 4.6 on Bedrock (ap-south-1) |

Docs: <https://skamalj.github.io/agentstate-reducer/langgraph/dynamodb/> and
<https://skamalj.github.io/agent-wait/>.

## Run

```bash
uv venv -p 3.12
uv sync --all-groups
export AWS_PROFILE=<your sso profile> AWS_DEFAULT_REGION=ap-south-1

uv run python act1_vanishes.py   | tee run-act1.log        # InMemorySaver: the question is gone
uv run python act2_survives.py   | tee run-act2.log        # DynamoDBSaver: park, kill, resume
uv run python act3_asks.py       | tee run-act3.log        # @wait + publish_interrupts: queue + row
uv run pytest -q
```

The model is real Bedrock, so all of the above needs credentials. DynamoDB and SQS are
not: `local_aws.py` starts `moto_server` and points `AWS_ENDPOINT_URL_DYNAMODB` and
`AWS_ENDPOINT_URL_SQS` at it, so **nothing is created in your account** and there is
nothing to clean up. Point those variables somewhere else to change that:

```bash
# DynamoDB Local instead of moto
docker run -d -p 8000:8000 amazon/dynamodb-local
AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8000 uv run python act2_survives.py

# real DynamoDB and SQS in your account (creates tables and queues)
AWS_ENDPOINT_URL_DYNAMODB= AWS_ENDPOINT_URL_SQS= uv run python act2_survives.py
```

Act 1 needs neither: `InMemorySaver` is the whole point of it.

`act2` and `act3` run the graph in **child interpreters**, so "the process
died" is a process exit and not a comment. The parent plays the consumer: it reads the
envelope off the queue and sends the answer back.

## The files

| file | what it is |
|---|---|
| `graph.py` | the agent: a model, `issue_refund` (limit enforced in the body), and `escalate_refund` behind `@wait(FINANCE)` |
| `act1_vanishes.py` | `InMemorySaver`, park, new interpreter, the question is gone |
| `act2_survives.py` | `DynamoDBSaver`, park, kill, resume; prints the DynamoDB items in between |
| `act3_asks.py` | `publish_interrupts` to a FIFO queue and an approvals row; answer, duplicate, late answer |
| `handler.py` | the Lambda host: one graph, one `if`, one publish to four announcers |
| `timeout_scheduler.py` | the consumer that turns `expires_at` into a one-shot EventBridge schedule |
| `cdk/` | three FIFO queues, two Lambdas, three tables, a schedule dead-letter queue |
| `deployed_run.py` | drives the deployed stack: the approve path, and `--timeout` for the deadline |
| `local_aws.py` | the `moto_server` that stands in for DynamoDB and SQS locally |
| `tests/` | the post's claims as assertions |
| `images/` | the post's diagrams, SVG source and 2x PNG; `check_diagrams.py` is the gate that keeps their numbers honest |

## Diagrams

Every number and id in `images/*.svg` (REPORT durations, envelope ids, the parked thread's PK
and byte sizes, the two TTL timestamps) is read back out of `blog.md` and asserted to be in
the SVG and in the image's alt text: `uv run python check_diagrams.py` (exit 1 on any drift).
Re-run it whenever a log is regenerated. The mechanics are generic; pass another post's
folder as the argument and it runs whichever rules apply there, and that post adds its own.

## Deploy

```bash
# build the bundle for the Lambda runtime (no docker needed)
uv pip install --target build/lambda --python-platform x86_64-manylinux_2_28 \
  --python-version 3.12 --no-installer-metadata --only-binary=:all: \
  "agent-wait[langgraph,aws]==0.7.0" "langgraph-dynamodb-checkpoint==0.5.0" "langchain>=1.0" langchain-aws
cp graph.py handler.py timeout_scheduler.py build/lambda/

cd cdk
# PT2M so a deadline can actually be watched to pass; P3D is the real policy
npx --yes aws-cdk@2 deploy --require-approval never -c bundlePath=../build/lambda   -c waitTimeout=PT2M --outputs-file outputs.json
cd .. && uv run python deployed_run.py --timeout | tee run-deployed-timeout.log
uv run python deployed_run.py | tee run-deployed.log

cd cdk && npx --yes aws-cdk@2 destroy --force      # tear it all down
```

`cdk/outputs.json` holds queue URLs, which contain your account id; it is gitignored.

### IAM

The Lambda role, as the stack writes it:

- `dynamodb:GetItem / PutItem / Query / UpdateItem / BatchWriteItem` on the three tables
  (the checkpoint table, the approvals table, the side-effect counter)
- `sqs:SendMessage` on the questions queue, and `ReceiveMessage / DeleteMessage /
  GetQueueAttributes` on the answers queue, from the event source
- `bedrock:InvokeModel` on `*` — narrow this to your inference profile

The timeout scheduler's role is separate and smaller:

- `scheduler:CreateSchedule / GetSchedule / DeleteSchedule`, scoped to `refund-timeout-*`
- `iam:PassRole` on the one role EventBridge Scheduler assumes, with an
  `iam:PassedToService` condition — the permission easiest to leave wide open and worst to
- that assumed role can `sqs:SendMessage` to the answers queue and its dead-letter queue,
  and nothing else

One thing to know: `DynamoDBSaver` creates its table if it is missing, which is why acts 2
and 3 need no setup. A role that cannot call `CreateTable` needs the table to exist
already, so the stack creates it — `PK`/`SK` (uppercase), string, no GSI, with `ttl` as
the TTL attribute. The package enables table TTL only on a table it created itself, so a
pre-created table has to enable it, as the stack does.

## What actually ran

All of this ran on 2026-09-24, from Windows 11 against `ap-south-1`, against these versions:

| package | version |
|---|---|
| `langgraph-dynamodb-checkpoint` | 0.5.0 |
| `agent-wait[langgraph,aws]` | 0.7.0 |
| `langgraph` | 1.2.12 |
| `langgraph-checkpoint` | 4.2.0 |
| `langchain` | 1.4.2 |
| `langchain-aws` | 1.7.9 |
| `boto3` | 1.43.101 |
| `moto` | 5.2.3 (server mode) |
| `pytest` | 9.1.1 |
| `aws-cdk-lib` | 2.270.0 |
| model | `global.anthropic.claude-sonnet-4-6` on Bedrock, `temperature=0` |

### Local acts (`moto_server` for DynamoDB and SQS, real Bedrock)

| act | log | what it produced |
|---|---|---|
| 1, `InMemorySaver` | `run-act1.log` | parked on 1 question; the fresh interpreter's resume produced a 1-message thread and a greeting; refunds issued: 0 |
| 2, `DynamoDBSaver` | `run-act2.log` | 8 items on `PK=order-4471-a2d5f9f5` while parked — 3 checkpoints (384 / 700 / 2172 bytes) and 5 pending writes, the last being `channel='__interrupt__'`, 496 bytes; resumed in a different pid; 14 items afterwards |
| 3, `@wait` + `publish_interrupts` | `run-act3.log` | 1 envelope on the questions queue, 1 row in the approvals table; approve → refund; the same answer again and a later `reject` → nothing; DynamoDB refund counter across all four host runs: **1** |

Each act runs the graph in child interpreters; the pids in the logs are different processes.

### Deployed (`ap-south-1`, tagged `project=agent-wait-demo`, destroyed after the run)

Stack: 3 FIFO queues + 1 FIFO DLQ + 1 standard schedule-DLQ, 2 Lambdas (the agent at
1024 MB, the timeout scheduler at 256 MB), 3 DynamoDB tables, and an IAM role EventBridge
Scheduler assumes. Deployed with `-c waitTimeout=PT2M` so a deadline can be watched to
pass. Bundle 47.4 MB zipped, built with `uv pip install --target` for
`x86_64-manylinux_2_28`; no docker anywhere.

Both scenarios below ran against the same deploy, so the figures are comparable.

**The approve path** (`run-deployed.log`, thread `order-d07f6e`):

| invocation | what it was | duration | billed |
|---|---|---|---|
| 1 | the customer's message; parks, publishes | 1706.21 ms | 1707 ms |
| 2 | finance approves; the refund runs | 1864.71 ms | 1865 ms |
| 3 | the same answer again | 31.37 ms | 32 ms |
| 4 | a `reject` after the fact | 16.29 ms | 17 ms |

181–182 MB of 1024, and no init duration on any of them: the timeout run went first on
the same function, so the container was warm. Refund counter **1** after all four —
which also confirms that enabling content-based deduplication (below) did not start
swallowing the duplicate answers, since every sender here passes an explicit
deduplication id.

**The deadline path** (`run-deployed-timeout.log`, thread `order-5ed76d`): the question
was published at 05:37:55 with `expires_at` 05:39:55; the scheduler Lambda created
`refund-timeout-95e310aa4663f0ec685aa157` (one-shot, `ActionAfterCompletion=DELETE`);
the schedule fired and deleted itself; the agent resumed at **05:40:13Z, 18 s after
`expires_at`**. Refund counter **0** after the deadline, still **0** after a late human
approval, and **0** messages on the schedule dead-letter queue.

Total: 3.6 GB-seconds across the approve path's four requests, plus the deadline path's
three agent invocations and one scheduler invocation. `run-deployed-lambda.log` holds the
agent's `REPORT` lines for both scenarios; the scheduler function's own were not captured
before its log group was deleted, so no duration is quoted for it. No invocation in this
deploy shows an `Init Duration` — the function was warm throughout — so there is no
cold-start figure here either.

#### The bug this run found

The first deployed attempt did not work, and the failure mode is worth the space. A FIFO
queue rejects a `SendMessage` that carries no `MessageDeduplicationId` unless
content-based deduplication is enabled, and EventBridge Scheduler's `SqsParameters` can
set `MessageGroupId` and nothing else. So Scheduler's delivery to `answers.fifo` was
refused — while the schedule itself completed and deleted itself, because from
Scheduler's side the invocation had been made. The result was a deadline that silently
never arrived, on a question that would have waited for ever, with the schedule already
tidied away.

The fix is `content_based_deduplication=True` on the answers queue. What makes it
visible, rather than fixed-by-luck next time, is a `DeadLetterConfig` on the schedule's
*target*: the answers queue's own DLQ could never have caught this, because a DLQ catches
messages that arrived and failed to process, and this was a send rejected before anything
entered the queue.

### Tests


`uv run pytest -q`, against `moto_server` and real Bedrock (`run-pytest.log`):

```text
................                                                         [100%]
16 passed in 109.91s (0:01:49)
```

| test | asserts |
|---|---|
| `test_the_run_parks_instead_of_refunding` | the question is `escalate_refund`; no refund while it is open |
| `test_a_fresh_process_resumes_the_thread` | the park and the resume are different pids, and the refund ran in the second |
| `test_the_refund_runs_exactly_once` | DynamoDB counter is 1 after approve, approve, reject |
| `test_a_duplicate_answer_runs_nothing` | neither repeat produced a refund |
| `test_the_envelope_carries_the_documented_fields` | `type`, `question`, `allowed_actions`, `tags`, `default`, `source`, `reply_with`, `expires_at` |
| `test_the_question_is_also_a_row` | `status="open"` and `expires_at` in the approvals table match the envelope |
| `test_the_deadline_default_is_an_ordinary_answer` | sending `envelope["default"]` back rejects; counter stays 0 |
| `test_under_the_limit_nothing_is_asked` | a small refund runs without any question |
| `test_ttl_seconds_stamps_every_parked_item` | every item has `ttl`; table TTL is `ENABLED` on `ttl` |
| `test_the_limit_is_code_not_a_prompt` | `issue_refund` called directly with 41,000 refuses and points at `escalate_refund`; no refund, counter stays 0; the same tool still pays 1,800 |

`tests/test_timeout_scheduler.py` is the deadline consumer, and it needs **no AWS account
at all** — moto's EventBridge Scheduler backend with dummy credentials:

| test | asserts |
|---|---|
| `test_it_schedules_the_default_at_the_deadline` | `at()` matches `expires_at`, target arn/role, `MessageGroupId`, `ActionAfterCompletion=DELETE`, and an `Input` that is exactly `{thread_id, question_id, answer=default}` |
| `test_a_republished_envelope_does_not_create_a_second_timer` | the `question_id`-derived name means one question gets one timer |
| `test_a_policy_with_no_timeout_is_skipped` | `expires_at: null` creates nothing |
| `test_a_deadline_already_past_is_sent_now` | a stale envelope sends the default immediately instead of scheduling into the past |
| `test_the_schedule_name_fits_the_limit` | the truncated name stays inside Scheduler's 64 characters |
| `test_without_a_dead_letter_queue_it_still_schedules_and_says_so` | `DeadLetterConfig` is optional: a missing arn neither crashes the consumer nor disappears quietly — the schedule is created and a warning is logged |

What these cannot cover is the schedule *firing* — moto does not run schedules. That half
is proved only by `run-deployed-timeout.log`.

Each test is a real model call and real interpreters, so the wording in your run differs.

## Honest limits

From `langgraph-dynamodb-checkpoint` 0.5.0:

- one item holds the whole serialized checkpoint, so DynamoDB's 400 KB item limit applies
  to large state or an unbounded message list. The optional `reducer=` is what bounds it;
  without it the package logs one INFO line per process about unbounded history
  (`AGENTSTATE_QUIET=1` silences it). Every run log here has that line in it.
- `delete_for_runs` is a filtered `Scan` and only finds items written by 0.5.0 or later.
- `prune(strategy="keep_latest")` is not `DeltaChannel`-aware.
- the async API is executor-backed, not native `aioboto3`.
- `ttl_seconds` stamps `ttl` on every item it writes; the package enables table TTL only
  when it creates the table.

From `agent-wait` 0.7.0:

- `expires_at` is advisory and measured from publish time. Nothing enforces it.
- `DynamoDbAnnounce` is an unconditional `put_item`, so a host that uses the row as a
  ledger must gate republishing itself.
- FIFO deduplication is a five-minute window, not a ledger. The envelope's `dedupe_key`
  is what a consumer deduplicates on.
- one `interrupt()` per node on langgraph 1.2.x — so one waiting tool per node.
- `@wait` is not conditional: it parks every call to the function it decorates. That is
  why `graph.py` has two tools rather than one tool with an `if`.
