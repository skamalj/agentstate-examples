#!/usr/bin/env python3
"""The CDK app. One stack, parameterised through context.

    npx aws-cdk deploy -c bundlePath=../build/lambda -c waitTimeout=PT3M
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from refund_stack import RefundApprovalStack

app = cdk.App()

RefundApprovalStack(
    app,
    app.node.try_get_context("stackName") or "approval-on-lambda",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="Human-in-the-loop LangGraph refund agent on Lambda (blog: approval-on-lambda)",
)

app.synth()
