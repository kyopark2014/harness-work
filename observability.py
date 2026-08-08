"""AgentCore Observability setup: CloudWatch Transaction Search for Harness.

Managed Harness emits traces/logs/metrics automatically. The account-level
prerequisite is Transaction Search (aws/spans). Runtime TRACES delivery and
custom ADOT instrumentation are not required for Harness.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

SPANS_LOG_GROUP = "aws/spans"
RESOURCE_POLICY_NAME = "TransactionSearchXRayAccess"
DESTINATION_WAIT_SECONDS = 900
DESTINATION_POLL_INTERVAL = 15


def _need_resource_policy(logs_client) -> bool:
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = logs_client.describe_resource_policies(**kwargs)
            for policy in response.get("resourcePolicies", []):
                if policy.get("policyName") == RESOURCE_POLICY_NAME:
                    return False
            next_token = response.get("nextToken")
            if not next_token:
                return True
    except Exception:
        return True


def _need_trace_destination(xray_client) -> bool:
    try:
        response = xray_client.get_trace_segment_destination()
        return response.get("Destination") != "CloudWatchLogs"
    except Exception:
        return True


def _need_indexing_rule(xray_client) -> bool:
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["NextToken"] = next_token
            response = xray_client.get_indexing_rules(**kwargs)
            for rule in response.get("IndexingRules", []):
                if rule.get("Name") == "Default":
                    return False
            next_token = response.get("NextToken")
            if not next_token:
                return True
    except Exception:
        return True


def spans_log_group_exists(region: str) -> bool:
    logs_client = boto3.client("logs", region_name=region)
    try:
        response = logs_client.describe_log_groups(
            logGroupNamePrefix=SPANS_LOG_GROUP, limit=1
        )
        return any(
            group.get("logGroupName") == SPANS_LOG_GROUP
            for group in response.get("logGroups", [])
        )
    except (ClientError, BotoCoreError) as error:
        logger.warning(
            "Failed to check for %s log group in %s: %s",
            SPANS_LOG_GROUP,
            region,
            error,
        )
        return False


def _get_trace_destination_status(xray_client, destination: str) -> tuple[str, bool]:
    try:
        response = xray_client.get_trace_segment_destination()
    except (ClientError, BotoCoreError) as error:
        logger.warning("get_trace_segment_destination failed: %s", error)
        return "UNKNOWN", False
    status = response.get("Status", "UNKNOWN")
    current = response.get("Destination")
    return status, (current == destination and status == "ACTIVE")


def _wait_for_trace_destination(xray_client, destination: str) -> str:
    deadline = time.time() + DESTINATION_WAIT_SECONDS
    status = "UNKNOWN"
    while time.time() < deadline:
        status, ready = _get_trace_destination_status(xray_client, destination)
        if ready:
            return status
        time.sleep(DESTINATION_POLL_INTERVAL)
    status, _ = _get_trace_destination_status(xray_client, destination)
    return status if status != "UNKNOWN" else "TIMEOUT"


def _create_cloudwatch_logs_resource_policy(
    logs_client, account_id: str, region: str
) -> None:
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:aws/spans:*",
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/application-signals/data:*",
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account_id}:*"},
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }
    logs_client.put_resource_policy(
        policyName=RESOURCE_POLICY_NAME,
        policyDocument=json.dumps(policy_document),
    )


def _configure_trace_segment_destination(xray_client) -> str:
    try:
        xray_client.update_trace_segment_destination(Destination="CloudWatchLogs")
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidRequestException":
            raise
    return _wait_for_trace_destination(xray_client, "CloudWatchLogs")


def _configure_indexing_rule(xray_client) -> None:
    try:
        xray_client.update_indexing_rule(
            Name="Default",
            Rule={"Probabilistic": {"DesiredSamplingPercentage": 1.0}},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidRequestException":
            raise


def _toggle_transaction_search_for_spans_log_group(region: str) -> str:
    xray_client = boto3.client("xray", region_name=region)
    print("  Toggling Transaction Search to create aws/spans log group...")
    try:
        xray_client.update_trace_segment_destination(Destination="XRay")
        xray_status = _wait_for_trace_destination(xray_client, "XRay")
        print(f"  X-Ray destination status: {xray_status}")
        xray_client.update_trace_segment_destination(Destination="CloudWatchLogs")
        cw_status = _wait_for_trace_destination(xray_client, "CloudWatchLogs")
        print(f"  CloudWatchLogs destination status: {cw_status}")
        return cw_status
    except (ClientError, BotoCoreError) as error:
        logger.warning(
            "Failed to toggle Transaction Search for spans log group in %s: %s",
            region,
            error,
        )
        return "ERROR"


def ensure_transaction_search(region: str, account_id: str) -> dict[str, Any]:
    """Enable Transaction Search prerequisites for AgentCore Observability."""
    result: dict[str, Any] = {"status": "success", "steps": []}
    logs_client = boto3.client("logs", region_name=region)
    xray_client = boto3.client("xray", region_name=region)

    if _need_resource_policy(logs_client):
        _create_cloudwatch_logs_resource_policy(logs_client, account_id, region)
        result["steps"].append("resource_policy")
    else:
        print("  CloudWatch Logs resource policy already configured")

    if _need_trace_destination(xray_client):
        status = _configure_trace_segment_destination(xray_client)
        result["steps"].append("trace_destination")
        result["destination_status"] = status
    else:
        response = xray_client.get_trace_segment_destination()
        result["destination_status"] = response.get("Status")
        print(
            f"  X-Ray trace destination already configured ({result['destination_status']})"
        )

    if _need_indexing_rule(xray_client):
        _configure_indexing_rule(xray_client)
        result["steps"].append("indexing_rule")
    else:
        print("  X-Ray indexing rule already configured")

    if not spans_log_group_exists(region):
        print("  aws/spans log group not found; toggling Transaction Search")
        result["destination_status"] = _toggle_transaction_search_for_spans_log_group(
            region
        )
        result["steps"].append("spans_log_group_toggle")
    else:
        print("  aws/spans log group exists")

    try:
        observability_client = boto3.client("observabilityadmin", region_name=region)
        status = observability_client.get_telemetry_evaluation_status().get("Status")
        if status == "NOT_STARTED":
            observability_client.start_telemetry_evaluation()
            result["steps"].append("telemetry_evaluation_started")
            print("  Started CloudWatch telemetry evaluation")
    except Exception as error:
        result["telemetry_evaluation_warning"] = str(error)

    if not spans_log_group_exists(region):
        result["status"] = "pending"
        result["warning"] = (
            "aws/spans log group is still missing. Harness traces may take up to "
            "10-15 minutes after Transaction Search becomes ACTIVE."
        )
    elif result.get("destination_status") not in (None, "ACTIVE"):
        result["status"] = "pending"
        result["warning"] = (
            "X-Ray trace destination is not ACTIVE yet. Harness traces may take up to "
            "10-15 minutes."
        )

    return result


def setup_agentcore_observability(region: str, account_id: str) -> dict[str, Any]:
    """Enable Transaction Search for Managed Harness observability."""
    print("  Enabling CloudWatch Transaction Search...")
    transaction_search = ensure_transaction_search(region, account_id)
    result: dict[str, Any] = {
        "transaction_search": transaction_search,
        "status": transaction_search.get("status", "success"),
    }
    if transaction_search.get("warning"):
        result["warning"] = transaction_search["warning"]
    return result
