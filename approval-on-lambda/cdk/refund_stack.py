"""Everything the deployed agent needs, and nothing it does not.

    answers.fifo ──► Lambda ──► checkpoints table   (the parked thread)
                        │  └──► approvals table     (what is open right now)
                        └─────► questions.fifo      (the envelope, for whoever answers)

Two FIFO queues, one Lambda, three tables. No scheduler, no wait store, no sweeper:
neither library keeps state of its own, so there is nothing else to run.

Both queues are FIFO and both legs set `MessageGroupId = thread_id`. Outbound that buys
deduplication -- `SqsAnnounce` sends the envelope's `dedupe_key` as
`MessageDeduplicationId`, so a republished question is swallowed by the queue. Inbound it
buys serialisation: two answers for one thread are never in flight at once, which on a
standard queue would mean two Lambdas resuming the same checkpoint.

The checkpointer would create its own table on first use. Here the stack creates it, so
the Lambda role can be scoped to three named tables instead of `dynamodb:CreateTable` on
the account.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_sqs as sqs
from constructs import Construct

KEYS = dict(
    partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
    sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
    billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
    removal_policy=RemovalPolicy.DESTROY,
)


class RefundApprovalStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bundle_path = self.node.try_get_context("bundlePath") or "../build/lambda"
        wait_timeout = self.node.try_get_context("waitTimeout") or "P3D"
        checkpoint_ttl = self.node.try_get_context("checkpointTtlSeconds")

        # ------------------------------------------------------------------ storage
        # The checkpointer's own table: PK/SK, uppercase, no GSI. `ttl` is the attribute
        # `DynamoDBSaver(ttl_seconds=...)` stamps; the package only enables TTL on a table
        # it created itself, so a pre-created table enables it here.
        checkpoints = dynamodb.Table(
            self,
            "Checkpoints",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Where DynamoDbAnnounce puts the open question. Nothing in agent-wait reads it.
        approvals = dynamodb.Table(self, "Approvals", **KEYS)
        # "Every open question, oldest deadline first" -- what an approvals UI queries, and
        # what a timeout sweep queries with a range condition on expires_at.
        approvals.add_global_secondary_index(
            index_name="by_status",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="expires_at", type=dynamodb.AttributeType.STRING),
        )

        # The side effect, counted. `ADD calls :one` is unconditional on purpose: if the
        # refund tool ever ran twice, this says 2.
        side_effects = dynamodb.Table(self, "SideEffects", **KEYS)

        # ------------------------------------------------------------------ queues
        dlq = sqs.Queue(self, "Dlq", fifo=True, retention_period=Duration.days(14))
        answers = sqs.Queue(  # inbound: starts and answers
            self,
            "Answers",
            fifo=True,
            content_based_deduplication=False,
            visibility_timeout=Duration.seconds(300),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
        questions = sqs.Queue(  # outbound: one envelope per question
            self, "Questions", fifo=True, content_based_deduplication=False,
            retention_period=Duration.days(4),
        )

        # ------------------------------------------------------------------ the agent
        env = {
            "REFUND_CHECKPOINT_TABLE": checkpoints.table_name,
            "REFUND_APPROVALS_TABLE": approvals.table_name,
            "REFUND_SIDE_EFFECT_TABLE": side_effects.table_name,
            "REFUND_QUESTIONS_QUEUE": questions.queue_url,
            "REFUND_WAIT_TIMEOUT": wait_timeout,
            "AGENTSTATE_QUIET": "1",
        }
        if checkpoint_ttl:
            env["REFUND_CHECKPOINT_TTL_SECONDS"] = str(checkpoint_ttl)

        function = lambda_.Function(
            self,
            "Run",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(bundle_path),
            handler="handler.handler",
            timeout=Duration.seconds(120),
            memory_size=1024,
            environment=env,
        )

        checkpoints.grant_read_write_data(function)
        approvals.grant_read_write_data(function)
        side_effects.grant_read_write_data(function)
        questions.grant_send_messages(function)
        function.add_to_role_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["*"])
        )

        # One thread, one message, one graph run.
        function.add_event_source(
            sources.SqsEventSource(answers, batch_size=1, report_batch_item_failures=True)
        )

        cdk.Tags.of(self).add("project", "agent-wait-demo")

        for name, value in {
            "AnswersQueueUrl": answers.queue_url,
            "QuestionsQueueUrl": questions.queue_url,
            "DlqUrl": dlq.queue_url,
            "CheckpointTable": checkpoints.table_name,
            "ApprovalsTable": approvals.table_name,
            "SideEffectTable": side_effects.table_name,
            "FunctionName": function.function_name,
            "LogGroup": function.log_group.log_group_name,
        }.items():
            cdk.CfnOutput(self, name, value=value)
