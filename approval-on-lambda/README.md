# approval-on-lambda

Code for the post *Externalize agent interrupts for HITL* (`blog.md` here) — human-in-the-loop
for LangGraph on AWS Lambda.

Read the post: <https://medium.com/@skamalj_11034/externalize-agent-interrupts-for-hitl-cb7566eff6a7>

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
uv run python two_clocks.py      | tee run-two-clocks.log  # expires_at vs ttl
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

`act2`, `act3` and `two_clocks` run the graph in **child interpreters**, so "the process
died" is a process exit and not a comment. The parent plays the consumer: it reads the
envelope off the queue and sends the answer back.

## The files

| file | what it is |
|---|---|
| `graph.py` | the agent: a model, `issue_refund` (limit enforced in the body), and `escalate_refund` behind `@wait(FINANCE)` |
| `act1_vanishes.py` | `InMemorySaver`, park, new interpreter, the question is gone |
| `act2_survives.py` | `DynamoDBSaver`, park, kill, resume; prints the DynamoDB items in between |
| `act3_asks.py` | `publish_interrupts` to a FIFO queue and an approvals row; answer, duplicate, late answer |
| `two_clocks.py` | `WaitPolicy(timeout="P3D")` against `DynamoDBSaver(ttl_seconds=86400)` |
| `handler.py` | the Lambda host: one graph, one `if`, one publish |
| `cdk/` | two FIFO queues, one Lambda, three tables |
| `deployed_run.py` | drives the deployed stack from a laptop and counts the refunds |
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
cp graph.py handler.py build/lambda/

cd cdk
npx --yes aws-cdk@2 deploy --require-approval never -c bundlePath=../build/lambda --outputs-file outputs.json
cd .. && uv run python deployed_run.py | tee run-deployed.log

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
| two clocks | `run-two-clocks.log` | `expires_at` 2026-09-27T18:19:38Z against a checkpoint `ttl` of 2026-09-25T18:19:37Z — the question outlives the thread by 2 days; after the items are deleted the approval produces a greeting and a refund counter of **0** |

Each act runs the graph in child interpreters; the pids in the logs are different processes.
All four logs were regenerated after the approval-limit guard was added to `issue_refund`,
so they match the code in this folder exactly.

### Deployed (`ap-south-1`, tagged `project=agent-wait-demo`, destroyed after the run)

Stack `approval-on-lambda`: 2 FIFO queues + 1 FIFO DLQ, 1 Lambda (Python 3.12, 1024 MB,
120 s timeout, 47.4 MB zipped bundle), 3 DynamoDB tables (`PAY_PER_REQUEST`). Bundle built
with `uv pip install --target` for `x86_64-manylinux_2_28`; no docker anywhere.

`run-deployed.log` is the driver's output, `run-deployed-lambda.log` the CloudWatch lines
(request ids stripped, no account ids). One approval, thread `order-fc56ea`:

| invocation | what it was | duration | billed | init |
|---|---|---|---|---|
| 1 | the customer's message; parks, publishes | 1642.21 ms | 5718 ms | 4075.36 ms |
| 2 | finance approves; the refund runs | 1697.55 ms | 1698 ms | warm |
| 3 | the same answer again | 15.31 ms | 16 ms | warm |
| 4 | a `reject` after the fact | 14.45 ms | 15 ms | warm |

Max memory used: 178–179 MB of 1024. The question appeared on the questions queue 7.1 s
after the customer's message was sent (SQS delivery + cold start + model call). The
DynamoDB refund counter for the order was **1** after all four.

`refunds_by_this_container` reads `["order-fc56ea"]` on invocations 3 and 4 because it is
module state in a reused container — nothing ran in either. The DynamoDB counter is the
one that means anything.

The approvals row for the thread is still `status="open"` after the refund has run, on a
table that has seen exactly one question. `DynamoDbAnnounce` writes the row and never
touches it again; closing it is the host's job and this host does not do it.

Total: 7.4 GB-seconds of Lambda across four requests, ~20 DynamoDB writes, 8 SQS messages,
2 Bedrock calls. The bundle that ran was built from the `graph.py` and `handler.py` in this
folder. Destroyed with `npx aws-cdk destroy --force` after the run; nothing is left
running.

### Tests

`uv run pytest -q`, against `moto_server` and real Bedrock (`run-pytest.log`):

```text
..........                                                               [100%]
10 passed in 68.31s (0:01:08)
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
