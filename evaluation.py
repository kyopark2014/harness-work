"""AgentCore Evaluations setup for Managed Harness (online evaluation config)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_EVALUATORS = [
    "Builtin.Helpfulness",
    "Builtin.GoalSuccessRate",
    "Builtin.ToolSelectionAccuracy",
]
DEFAULT_SAMPLING_PERCENTAGE = 10.0
# Idle minutes before online evaluation treats a session as complete.
DEFAULT_SESSION_TIMEOUT_MINUTES = 5
ROLE_PROPAGATION_INITIAL_WAIT_SECONDS = 5
ROLE_PROPAGATION_MAX_ATTEMPTS = 8
ROLE_PROPAGATION_BASE_DELAY_SECONDS = 2
ROLE_PROPAGATION_MAX_DELAY_SECONDS = 15

T = TypeVar("T")


def evaluation_role_name(project_name: str) -> str:
    return f"AmazonBedrockAgentCoreEvaluationRoleFor{project_name}"


def online_evaluation_config_name(project_name: str) -> str:
    safe_name = project_name.replace("-", "_")
    return f"{safe_name}_harness_online_eval"


def _runtime_base_name(runtime_arn: str) -> str:
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    if "-" in runtime_id:
        return runtime_id.rsplit("-", 1)[0]
    return runtime_id


def harness_service_name(runtime_arn: str, qualifier: str = "DEFAULT") -> str:
    """OTEL service.name for the Harness backing AgentCore Runtime."""
    return f"{_runtime_base_name(runtime_arn)}.{qualifier}"


def harness_traces_log_group(runtime_arn: str, qualifier: str = "DEFAULT") -> str:
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{qualifier}"


def _evaluation_trust_policy(account_id: str, region: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TrustPolicyStatement",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id,
                        "aws:ResourceAccount": account_id,
                    },
                    "ArnLike": {
                        "aws:SourceArn": [
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:evaluator/*",
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:online-evaluation-config/*",
                        ]
                    },
                },
            },
        ],
    }


def _evaluation_permissions_policy(region: str, account_id: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CloudWatchLogReadStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:DescribeLogGroups",
                    "logs:GetQueryResults",
                    "logs:StartQuery",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                ],
                "Resource": "*",
            },
            {
                "Sid": "CloudWatchLogWriteStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": (
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/evaluations/*"
                ),
            },
            {
                "Sid": "CloudWatchIndexPolicyStatement",
                "Effect": "Allow",
                "Action": [
                    "logs:DescribeIndexPolicies",
                    "logs:PutIndexPolicy",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans",
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans:*",
                ],
            },
            {
                "Sid": "BedrockInvokeStatement",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                ],
            },
        ],
    }


def _upsert_iam_policy(
    iam_client,
    policy_name: str,
    policy_document: dict[str, Any],
    account_id: str,
) -> str:
    policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
    document = json.dumps(policy_document)

    try:
        iam_client.get_policy(PolicyArn=policy_arn)
        versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
        if len(versions) >= 5:
            for version in versions:
                if not version["IsDefaultVersion"]:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=version["VersionId"],
                    )
                    break
        iam_client.create_policy_version(
            PolicyArn=policy_arn,
            PolicyDocument=document,
            SetAsDefault=True,
        )
        print(f"  Updated IAM policy: {policy_name}")
    except iam_client.exceptions.NoSuchEntityException:
        response = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument=document,
            Description="Permissions for Amazon Bedrock AgentCore Evaluations",
        )
        policy_arn = response["Policy"]["Arn"]
        print(f"  Created IAM policy: {policy_name}")

    return policy_arn


def _is_execution_role_assume_error(error: ClientError) -> bool:
    err = error.response.get("Error", {})
    code = err.get("Code", "")
    message = str(err.get("Message", "")).lower()
    return code == "ValidationException" and "cannot be assumed" in message


def _with_iam_propagation_retry(action: str, fn: Callable[[], T]) -> T:
    last_error: ClientError | None = None
    for attempt in range(1, ROLE_PROPAGATION_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except ClientError as error:
            if not _is_execution_role_assume_error(error):
                raise
            last_error = error
            if attempt >= ROLE_PROPAGATION_MAX_ATTEMPTS:
                break
            delay = min(
                ROLE_PROPAGATION_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                ROLE_PROPAGATION_MAX_DELAY_SECONDS,
            )
            print(
                f"  Waiting {delay}s for evaluation role IAM propagation "
                f"before retrying {action} "
                f"(attempt {attempt}/{ROLE_PROPAGATION_MAX_ATTEMPTS})"
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def ensure_evaluation_execution_role(
    region: str,
    account_id: str,
    project_name: str,
) -> tuple[str, bool]:
    """Create or update the IAM role used by AgentCore Evaluations."""
    iam_client = boto3.client("iam")
    role_name = evaluation_role_name(project_name)
    policy_name = f"{role_name}Policy"
    newly_created = False

    policy_arn = _upsert_iam_policy(
        iam_client,
        policy_name,
        _evaluation_permissions_policy(region, account_id),
        account_id,
    )

    trust_policy = json.dumps(_evaluation_trust_policy(account_id, region))
    try:
        iam_client.get_role(RoleName=role_name)
        iam_client.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=trust_policy,
        )
        print(f"  Existing evaluation role found: {role_name}")
    except iam_client.exceptions.NoSuchEntityException:
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=trust_policy,
            Description="Execution role for Amazon Bedrock AgentCore Evaluations",
        )
        newly_created = True
        print(f"  Created evaluation role: {role_name}")

    iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return f"arn:aws:iam::{account_id}:role/{role_name}", newly_created


def _find_online_evaluation_config(client, config_name: str) -> dict[str, Any] | None:
    next_token = None
    while True:
        params: dict[str, Any] = {}
        if next_token:
            params["nextToken"] = next_token
        try:
            response = client.list_online_evaluation_configs(**params)
        except ClientError:
            logger.exception(
                "Failed to list online evaluation configs while looking up %s",
                config_name,
            )
            return None
        for item in response.get("onlineEvaluationConfigs", []):
            if item.get("onlineEvaluationConfigName") == config_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def _evaluation_rule(
    sampling_percentage: float,
    session_timeout_minutes: int,
) -> dict[str, Any]:
    return {
        "samplingConfig": {"samplingPercentage": sampling_percentage},
        "sessionConfig": {"sessionTimeoutMinutes": session_timeout_minutes},
    }


def _cloudwatch_data_source_config(
    log_group: str,
    service_name: str,
) -> dict[str, Any]:
    return {
        "cloudWatchLogs": {
            "logGroupNames": [log_group],
            "serviceNames": [service_name],
        }
    }


def ensure_online_evaluation_config(
    runtime_arn: str,
    region: str,
    account_id: str,
    project_name: str,
    execution_role_arn: str,
    sampling_percentage: float = DEFAULT_SAMPLING_PERCENTAGE,
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES,
    evaluators: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update online evaluation for the Harness backing Runtime."""
    config_name = online_evaluation_config_name(project_name)
    service_name = harness_service_name(runtime_arn)
    log_group = harness_traces_log_group(runtime_arn)
    evaluator_ids = evaluators or DEFAULT_EVALUATORS
    rule = _evaluation_rule(sampling_percentage, session_timeout_minutes)
    data_source_config = _cloudwatch_data_source_config(log_group, service_name)

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    existing = _find_online_evaluation_config(client, config_name)
    if existing:
        config_id = existing["onlineEvaluationConfigId"]

        def _update() -> None:
            client.update_online_evaluation_config(
                onlineEvaluationConfigId=config_id,
                rule=rule,
                dataSourceConfig=data_source_config,
                evaluationExecutionRoleArn=execution_role_arn,
                evaluators=[
                    {"evaluatorId": evaluator_id} for evaluator_id in evaluator_ids
                ],
            )

        _with_iam_propagation_retry("update_online_evaluation_config", _update)
        print(
            f"  Updated online evaluation config: {config_name} "
            f"(sessionTimeoutMinutes={session_timeout_minutes}, "
            f"logGroup={log_group}, serviceName={service_name})"
        )
        return {
            "status": "success",
            "online_evaluation_config_name": config_name,
            "online_evaluation_config_id": config_id,
            "configured": True,
            "service_name": service_name,
            "log_group": log_group,
            "evaluators": evaluator_ids,
            "sampling_percentage": sampling_percentage,
            "session_timeout_minutes": session_timeout_minutes,
        }

    def _create() -> dict[str, Any]:
        return client.create_online_evaluation_config(
            onlineEvaluationConfigName=config_name,
            description=f"Online evaluation for Managed Harness ({project_name})",
            rule=rule,
            dataSourceConfig=data_source_config,
            evaluators=[{"evaluatorId": evaluator_id} for evaluator_id in evaluator_ids],
            evaluationExecutionRoleArn=execution_role_arn,
            enableOnCreate=True,
        )

    response = _with_iam_propagation_retry("create_online_evaluation_config", _create)
    print(
        f"  Created online evaluation config: {config_name} "
        f"(sessionTimeoutMinutes={session_timeout_minutes})"
    )
    return {
        "status": "success",
        "online_evaluation_config_name": config_name,
        "online_evaluation_config_id": response.get("onlineEvaluationConfigId"),
        "configured": True,
        "service_name": service_name,
        "log_group": log_group,
        "evaluators": evaluator_ids,
        "sampling_percentage": sampling_percentage,
        "session_timeout_minutes": session_timeout_minutes,
    }


def setup_agentcore_evaluation(
    runtime_arn: str | None,
    region: str,
    account_id: str,
    project_name: str,
) -> dict[str, Any]:
    """Create evaluation IAM role and online evaluation configuration."""
    result: dict[str, Any] = {"status": "success"}

    if not runtime_arn:
        result["status"] = "skipped"
        result["warning"] = "harness_runtime_arn not found; skipped evaluation setup"
        return result

    print("  Creating evaluation execution role...")
    execution_role_arn, role_created = ensure_evaluation_execution_role(
        region, account_id, project_name
    )
    result["evaluation_execution_role_arn"] = execution_role_arn

    if role_created:
        print(
            f"  Waiting {ROLE_PROPAGATION_INITIAL_WAIT_SECONDS}s for new evaluation "
            "role to become assumable..."
        )
        time.sleep(ROLE_PROPAGATION_INITIAL_WAIT_SECONDS)

    print("  Creating online evaluation configuration...")
    evaluation = ensure_online_evaluation_config(
        runtime_arn=runtime_arn,
        region=region,
        account_id=account_id,
        project_name=project_name,
        execution_role_arn=execution_role_arn,
    )
    result.update(evaluation)
    return result
