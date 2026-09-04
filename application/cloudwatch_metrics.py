"""CloudWatch custom metrics and dashboard helpers for AgentCore Harness."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Strip inference-profile / vendor prefixes before publishing ModelId dimension.
_MODEL_ID_PREFIXES = (
    "us.anthropic.",
    "us.amazon.",
    "eu.anthropic.",
    "eu.amazon.",
    "apac.anthropic.",
    "apac.amazon.",
    "global.anthropic.",
    "openai.",
    "anthropic.",
    "amazon.",
)
_MODEL_ID_REGION_VENDORS = frozenset(
    {"anthropic", "amazon", "meta", "mistral", "cohere", "ai21", "openai"}
)
# e.g. claude-haiku-4-5-20251001-v1:0 → claude-haiku-4-5
_MODEL_ID_DATED_VERSION_RE = re.compile(r"-\d{8}-v\d+(?::\d+)?$")
_MODEL_ID_VERSION_SUFFIX_RE = re.compile(r"-v\d+:\d+$")
# CloudWatch dimension value soft cap for ModelId (well under the 1024-byte limit).
MODEL_ID_MAX_LENGTH = 64
# Project/dimension values embedded in Metrics Insights / SEARCH expressions.
_METRIC_DIM_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _sanitize_metric_dimension(value: str, *, field: str = "dimension") -> str:
    """Reject values unsafe to embed in CloudWatch query expressions.

    Metrics Insights / SEARCH do not support bind parameters, so callers must
    only interpolate validated identifiers (project name, metric name, etc.).
    """
    if not isinstance(value, str) or not _METRIC_DIM_SAFE_RE.fullmatch(value):
        raise ValueError(f"Unsafe CloudWatch metric {field}: {value!r}")
    return value


def _short_model_id(model_id: str) -> str:
    """Human-readable ModelId for CloudWatch dimensions and legends.

    Do not use ``rsplit('.', 1)`` — OpenAI IDs like ``openai.gpt-5.4`` and
    ``openai.gpt-5.6-terra`` would collapse to ``4`` / ``6-terra``.
    """
    if not model_id:
        return "unknown"

    short = model_id
    lowered = short.lower()
    for prefix in _MODEL_ID_PREFIXES:
        if lowered.startswith(prefix):
            short = short[len(prefix) :]
            break
    else:
        parts = short.split(".", 2)
        if (
            len(parts) == 3
            and parts[0] in ("us", "eu", "apac", "global")
            and parts[1].lower() in _MODEL_ID_REGION_VENDORS
        ):
            short = parts[2]

    short = _MODEL_ID_DATED_VERSION_RE.sub("", short)
    short = _MODEL_ID_VERSION_SUFFIX_RE.sub("", short)
    return short[:MODEL_ID_MAX_LENGTH] or "unknown"

METRIC_NAMESPACE = "Harness/AgentCore"
AGENTCORE_NAMESPACE = "AWS/Bedrock-AgentCore"
# Managed Harness sessions run on an internal AgentCore Runtime. Vended CPU /
# Memory / Invocations metrics use Runtime dimensions (not Harness ARN).
# See: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html
AGENTCORE_SERVICE = "AgentCore.Runtime"
BEDROCK_NAMESPACE = "AWS/Bedrock"
BEDROCK_USAGE_DASHBOARD_NAME = "Bedrock-Usage-Dashboard"
INVOKE_OPERATION = "InvokeAgentRuntime"

# Dashboard gauge axis maxima (monitoring thresholds, not hard service limits).
LATENCY_GAUGE_MAX_MS = 30000  # Expected upper bound for InvokeAgentRuntime p99 latency
# Gauge annotation thresholds (ms): green → amber → red for p99 latency.
LATENCY_GAUGE_WARN_MS = 5000
LATENCY_GAUGE_CRITICAL_MS = 15000
ACTIVE_SESSIONS_GAUGE_MAX = 50  # Expected max concurrent AgentCore sessions for capacity view
# Gauge annotation thresholds: green → amber → red for concurrent sessions.
ACTIVE_SESSIONS_GAUGE_WARN = 10
ACTIVE_SESSIONS_GAUGE_CRITICAL = 30

# AgentCore Runtime pricing (USD)
RUNTIME_CPU_COST_PER_VCPU_HOUR = 0.0895
RUNTIME_MEMORY_COST_PER_GB_HOUR = 0.00945
COST_DISPLAY_DECIMALS = 3

# Bedrock on-demand pricing per 1M tokens (USD). Used for estimated model cost.
MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "us.anthropic.claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "us.anthropic.claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-fable-5": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-fable-5-1": {"input": 10.0, "output": 50.0},
    "us.amazon.nova-premier-v1:0": {"input": 2.5, "output": 12.5},
    "us.amazon.nova-pro-v1:0": {"input": 0.80, "output": 3.20},
    "us.amazon.nova-lite-v1:0": {"input": 0.06, "output": 0.24},
    "us.amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14},
    "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
    "openai.gpt-5.4": {"input": 1.25, "output": 10.0},
    "openai.gpt-5.5": {"input": 1.25, "output": 10.0},
    "openai.gpt-5.6-sol": {"input": 5.0, "output": 30.0},
    "openai.gpt-5.6-terra": {"input": 2.5, "output": 15.0},
    "openai.gpt-5.6-luna": {"input": 1.0, "output": 6.0},
    "openai.gpt-oss-120b-1:0": {"input": 0.30, "output": 0.60},
    "openai.gpt-oss-20b-1:0": {"input": 0.10, "output": 0.30},
}

DEFAULT_MODEL_PRICING = {"input": 3.0, "output": 15.0}

_cloudwatch_client = None


def _get_cloudwatch_client():
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region)
    return _cloudwatch_client


def _harness_name(project_name: str) -> str:
    return project_name.replace("-", "_")


def _load_runtime_context() -> dict[str, str]:
    project_name = "harness-work"
    runtime_name = _harness_name("harness-work")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"

    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.json")
        if not os.path.isfile(config_path):
            config_path = os.path.join(script_dir, "..", "application", "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_name = config.get("projectName", project_name)
        region = config.get("region", region)
        runtime_name = _harness_name(project_name)
        arn = config.get("HARNESS_ARN") or config.get("harnessId", "")
        if arn:
            runtime_name = arn.rsplit("/", 1)[-1]
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    return {
        "ProjectName": project_name,
        "HarnessName": runtime_name,
        "Region": region,
    }


def _resolve_model_pricing(model_id: str) -> dict[str, float]:
    if model_id in MODEL_PRICING_PER_MILLION:
        return MODEL_PRICING_PER_MILLION[model_id]

    for key, pricing in MODEL_PRICING_PER_MILLION.items():
        if model_id.startswith(key) or key in model_id:
            return pricing

    return DEFAULT_MODEL_PRICING


def _uncached_and_cache_tokens(
    input_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> tuple[int, int, int]:
    """Split input footprint into uncached / cache_read / cache_creation.

    Some providers report ``input_tokens`` as the full footprint (uncached +
    cache), others as uncached-only. Prefer the non-double-counting split.
    """
    cache_read = max(0, cache_read)
    cache_creation = max(0, cache_creation)
    input_tokens = max(0, input_tokens)
    cache_total = cache_read + cache_creation
    if cache_total and input_tokens >= cache_total:
        uncached = input_tokens - cache_total
    else:
        uncached = input_tokens
    return uncached, cache_read, cache_creation


def _normalize_usage_dict(raw: dict[str, Any]) -> dict[str, int]:
    """Normalize Bedrock / Strands / LangChain usage keys to input/output/total/cache."""
    input_tokens = int(
        raw.get("input_tokens")
        or raw.get("inputTokens")
        or raw.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        raw.get("output_tokens")
        or raw.get("outputTokens")
        or raw.get("completion_tokens")
        or 0
    )
    total_tokens = int(
        raw.get("total_tokens")
        or raw.get("totalTokens")
        or input_tokens + output_tokens
    )
    cache_read = int(
        raw.get("cache_read")
        or raw.get("cache_read_input_tokens")
        or raw.get("cacheReadInputTokens")
        or 0
    )
    cache_creation = int(
        raw.get("cache_creation")
        or raw.get("cache_creation_input_tokens")
        or raw.get("cache_write_input_tokens")
        or raw.get("cacheWriteInputTokens")
        or 0
    )
    details = raw.get("input_token_details")
    if isinstance(details, dict):
        cache_read = cache_read or int(details.get("cache_read") or 0)
        cache_creation = cache_creation or int(details.get("cache_creation") or 0)

    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }
    return {k: v for k, v in usage.items() if v > 0}


def extract_token_usage(message: Any) -> dict[str, int]:
    """Extract token counts (including prompt-cache) from a Strands AgentResult or usage dict."""
    if message is None:
        return {}

    # Already a usage mapping (Strands Usage TypedDict or plain dict)
    if isinstance(message, dict):
        if any(
            key in message
            for key in (
                "inputTokens",
                "outputTokens",
                "totalTokens",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cacheReadInputTokens",
                "cacheWriteInputTokens",
            )
        ):
            return _normalize_usage_dict(message)

    # Strands AgentResult.metrics.accumulated_usage / latest invocation
    metrics = getattr(message, "metrics", None)
    if metrics is not None:
        latest = getattr(metrics, "latest_agent_invocation", None)
        if latest is not None:
            latest_usage = getattr(latest, "usage", None)
            if isinstance(latest_usage, dict) and latest_usage:
                normalized = _normalize_usage_dict(latest_usage)
                if normalized:
                    return normalized

        accumulated = getattr(metrics, "accumulated_usage", None)
        if isinstance(accumulated, dict) and accumulated:
            normalized = _normalize_usage_dict(accumulated)
            if normalized:
                return normalized

    # LangChain-style fallback (AIMessage)
    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        normalized = _normalize_usage_dict(usage_metadata)
        if normalized:
            return normalized

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        bedrock_usage = response_metadata.get("usage") or response_metadata.get("token_usage") or {}
        if isinstance(bedrock_usage, dict):
            normalized = _normalize_usage_dict(bedrock_usage)
            if normalized:
                return normalized
        # Top-level Converse / Bedrock response fields
        top_level = _normalize_usage_dict(response_metadata)
        if top_level:
            return top_level

    return {}


def estimate_model_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _resolve_model_pricing(model_id)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 8)


def publish_token_metrics(model_id: str, message: Any) -> None:
    """Publish token usage, prompt-cache, and estimated model cost to CloudWatch."""
    usage = extract_token_usage(message)
    if not usage:
        metrics = getattr(message, "metrics", None)
        logger.info(
            "No token usage on message; skip CloudWatch publish "
            "(model=%s accumulated_usage=%s latest_usage=%s)",
            model_id,
            getattr(metrics, "accumulated_usage", None) if metrics is not None else None,
            getattr(getattr(metrics, "latest_agent_invocation", None), "usage", None)
            if metrics is not None
            else None,
        )
        return

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
    if total_tokens <= 0:
        logger.info(  # nosemgrep: python.lang.security.audit.logging.python-logger-credential-disclosure
            "Token usage present but total_tokens<=0; skip CloudWatch publish (model=%s usage=%s)",
            model_id,
            usage,
        )
        return

    uncached, cache_read, cache_creation = _uncached_and_cache_tokens(
        input_tokens,
        usage.get("cache_read", 0),
        usage.get("cache_creation", 0),
    )
    input_footprint = uncached + cache_read + cache_creation
    cache_hit_ratio = (
        (100.0 * cache_read / input_footprint) if input_footprint > 0 else 0.0
    )

    context = _load_runtime_context()
    model_short = _short_model_id(model_id)
    dimensions = [
        {"Name": "ProjectName", "Value": context["ProjectName"]},
        {"Name": "HarnessName", "Value": context["HarnessName"]},
        {"Name": "ModelId", "Value": model_short},
    ]

    estimated_cost = estimate_model_cost_usd(model_id, input_tokens, output_tokens)
    timestamp = None
    try:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc)
    except Exception:
        pass

    metric_data = [
        {"MetricName": "InputTokens", "Value": float(input_tokens), "Unit": "Count"},
        {"MetricName": "OutputTokens", "Value": float(output_tokens), "Unit": "Count"},
        {"MetricName": "TotalTokens", "Value": float(total_tokens), "Unit": "Count"},
        {"MetricName": "EstimatedModelCostUSD", "Value": estimated_cost, "Unit": "None"},
        {"MetricName": "LLMInvocations", "Value": 1.0, "Unit": "Count"},
        {"MetricName": "CacheReadTokens", "Value": float(cache_read), "Unit": "Count"},
        {
            "MetricName": "CacheCreationTokens",
            "Value": float(cache_creation),
            "Unit": "Count",
        },
        {
            "MetricName": "UncachedInputTokens",
            "Value": float(uncached),
            "Unit": "Count",
        },
        {
            "MetricName": "CacheHitRatio",
            "Value": float(cache_hit_ratio),
            "Unit": "Percent",
        },
    ]

    for entry in metric_data:
        entry["Dimensions"] = dimensions
        if timestamp:
            entry["Timestamp"] = timestamp

    try:
        _get_cloudwatch_client().put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
        logger.info(  # nosemgrep: python.lang.security.audit.logging.python-logger-credential-disclosure
            "Published token metrics: model=%s input=%s output=%s "
            "cache_read=%s cache_creation=%s hit_ratio=%.1f%% cost=$%.6f",
            model_short,
            input_tokens,
            output_tokens,
            cache_read,
            cache_creation,
            cache_hit_ratio,
            estimated_cost,
        )
    except Exception as exc:
        logger.warning("Failed to publish CloudWatch token metrics: %s", exc)  # nosemgrep: python.lang.security.audit.logging.python-logger-credential-disclosure


def dashboard_name(project_name: str) -> str:
    safe_name = project_name.replace(" ", "-")
    return f"{safe_name}-monitoring"


def _is_agent_runtime_arn(arn: str) -> bool:
    return ":runtime/" in (arn or "")


def _is_harness_arn(arn: str) -> bool:
    return ":harness/" in (arn or "")


def _runtime_arn_from_harness_document(harness: dict[str, Any]) -> str | None:
    """Pull managed Harness backing Runtime ARN from GetHarness payload."""
    env = harness.get("environment") or {}
    runtime_env = env.get("agentCoreRuntimeEnvironment") or {}
    arn = runtime_env.get("agentRuntimeArn")
    return arn if isinstance(arn, str) and _is_agent_runtime_arn(arn) else None


def resolve_harness_runtime_arn(
    harness_or_runtime_arn: str,
    region: str,
    *,
    runtime_arn: str | None = None,
) -> str:
    """Resolve the Runtime ARN used by AWS/Bedrock-AgentCore vended metrics.

    Managed Harness vends Invocations / CPUUsed / MemoryUsed against the internal
    ``environment.agentCoreRuntimeEnvironment.agentRuntimeArn``, not the Harness ARN.
    """
    if runtime_arn and _is_agent_runtime_arn(runtime_arn):
        return runtime_arn
    if _is_agent_runtime_arn(harness_or_runtime_arn):
        return harness_or_runtime_arn

    # Prefer config.json when installer already stored the mapping.
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        cfg_runtime = (config.get("harness_runtime_arn") or "").strip()
        if _is_agent_runtime_arn(cfg_runtime):
            return cfg_runtime
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    if not _is_harness_arn(harness_or_runtime_arn):
        raise ValueError(
            "Expected a harness ARN (...:harness/...) or runtime ARN (...:runtime/...), "
            f"got: {harness_or_runtime_arn!r}"
        )

    harness_id = harness_or_runtime_arn.rsplit("/", 1)[-1]
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    harness = control.get_harness(harnessId=harness_id)["harness"]
    resolved = _runtime_arn_from_harness_document(harness)
    if not resolved:
        raise ValueError(
            f"Harness {harness_id!r} has no environment.agentCoreRuntimeEnvironment."
            "agentRuntimeArn; cannot bind vended CPU/Memory metrics"
        )
    return resolved


def _runtime_base_name(agent_runtime_arn: str) -> str:
    runtime_id = agent_runtime_arn.rsplit("/", 1)[-1]
    if "-" in runtime_id:
        return runtime_id.rsplit("-", 1)[0]
    return runtime_id


def _runtime_name_dimension(agent_runtime_arn: str) -> str:
    return f"{_runtime_base_name(agent_runtime_arn)}::DEFAULT"


def _metric_options(**options: Any) -> dict[str, Any]:
    return options


def _agentcore_invoke_metric(
    metric_name: str,
    agent_runtime_arn: str,
    **options: Any,
) -> list[Any]:
    """AgentCore Runtime invoke metric (Resource, Operation, Name)."""
    row: list[Any] = [
        AGENTCORE_NAMESPACE,
        metric_name,
        "Resource",
        agent_runtime_arn,
        "Operation",
        INVOKE_OPERATION,
        "Name",
        _runtime_name_dimension(agent_runtime_arn),
    ]
    if options:
        row.append(_metric_options(**options))
    return row


def _agentcore_resource_metric(
    metric_name: str,
    agent_runtime_arn: str,
    **options: Any,
) -> list[Any]:
    """AgentCore Runtime resource metric (Resource, Service, Name)."""
    row: list[Any] = [
        AGENTCORE_NAMESPACE,
        metric_name,
        "Resource",
        agent_runtime_arn,
        "Service",
        AGENTCORE_SERVICE,
        "Name",
        _runtime_name_dimension(agent_runtime_arn),
    ]
    if options:
        row.append(_metric_options(**options))
    return row


def _custom_metric_search_expression(
    metric_name: str,
    project_name: str,
    period: int,
    stat: str = "Sum",
) -> str:
    """SEARCH expression for custom metrics published with multiple dimensions."""
    project_name = _sanitize_metric_dimension(project_name, field="ProjectName")
    metric_name = _sanitize_metric_dimension(metric_name, field="MetricName")
    if not _METRIC_DIM_SAFE_RE.fullmatch(stat):
        raise ValueError(f"Unsafe CloudWatch metric stat: {stat!r}")
    return (
        f"SEARCH('{{{METRIC_NAMESPACE},ProjectName,HarnessName,ModelId}} "
        f'ProjectName="{project_name}" MetricName="{metric_name}"\', '
        f"'{stat}', {period})"
    )


def _custom_model_metric_query(
    metric_name: str,
    project_name: str,
    period: int,
    metric_id: str = "e1",
    stat: str = "Sum",
) -> list[list[dict[str, Any]]]:
    """SEARCH query with dynamic label showing ModelId only in legends.

    Metrics are published with ProjectName, HarnessName, and ModelId.
    Default SEARCH labels concatenate all dimension values
    (e.g. "runtime_xxx claude-fable-5"); keep ModelId only.
    """
    return [
        [
            {
                "expression": _custom_metric_search_expression(
                    metric_name, project_name, period, stat
                ),
                "label": "${PROP('Dim.ModelId')}",
                "id": metric_id,
            }
        ]
    ]



def _tokens_by_model_pie_metrics(
    project_name: str,
    period: int = 86400,
    region: str | None = None,
) -> list[list[dict[str, Any]]]:
    """Aggregate TotalTokens by ModelId for a pie chart.

    SEARCH on ProjectName+HarnessName+ModelId splits one model across
    runtimes and blank/zeros the pie. A single Metrics Insights
    ``GROUP BY ModelId`` aggregates correctly.

    Do not add ``ORDER BY`` — CloudWatch then prefixes legend labels with
    ``1 - ``, ``2 - ``. Do not use multiple Insights queries in one widget —
    that raises "error while trying to get graph data".
    """
    del region
    # Metrics Insights has no bind parameters; only interpolate sanitized ids.
    project_name = _sanitize_metric_dimension(project_name, field="ProjectName")
    # nosec B608: Metrics Insights (not DB SQL); project_name is regex-sanitized above.
    expression = (  # nosec B608 — CW Metrics Insights, not SQL injection surface
        f'SELECT SUM(TotalTokens) FROM SCHEMA("{METRIC_NAMESPACE}", '
        f"ProjectName, HarnessName, ModelId) "
        f"WHERE ProjectName = '{project_name}' "
        f"GROUP BY ModelId"
    )
    return [
        [
            {
                "expression": expression,
                "label": "",
                "id": "q1",
                "period": period,
            }
        ]
    ]


def _custom_project_metric(
    metric_name: str,
    project_name: str,
    period: int = 300,
    stat: str = "Sum",
    aggregate: bool = True,
    **options: Any,
) -> list[Any]:
    """Custom Strands metric query.

    Metrics are published with ProjectName, HarnessName, and ModelId.
    CloudWatch requires SEARCH (or full dimension match), not ProjectName alone.
    """
    search_expr = _custom_metric_search_expression(metric_name, project_name, period, stat)
    expression = f"SUM({search_expr})" if aggregate else search_expr
    row: dict[str, Any] = {"expression": expression}
    if aggregate and not options.get("id"):
        row["label"] = metric_name
    if options:
        row.update(_metric_options(**options))
    return [row]


def _estimated_cost_source_metrics(
    agent_runtime_arn: str,
    project_name: str,
    period: int = 300,
) -> list[Any]:
    """Hidden metrics used by estimated cost expressions."""
    return [
        _agentcore_resource_metric(
            "CPUUsed-vCPUHours", agent_runtime_arn, id="m1", visible=False
        ),
        _agentcore_resource_metric(
            "MemoryUsed-GBHours", agent_runtime_arn, id="m2", visible=False
        ),
        _custom_project_metric(
            "EstimatedModelCostUSD",
            project_name,
            period=period,
            id="m3",
            visible=False,
        ),
    ]


def _round_expression(expression: str, decimals: int = COST_DISPLAY_DECIMALS) -> str:
    """Round to fixed decimals using FLOOR (CloudWatch has no ROUND function)."""
    multiplier = 10**decimals
    return f"FLOOR(({expression}) * {multiplier} + 0.5) / {multiplier}"


def _round_cost_expression(expression: str) -> str:
    """Round USD cost displays to COST_DISPLAY_DECIMALS."""
    return _round_expression(expression, COST_DISPLAY_DECIMALS)


def _summary_cost_widget_options() -> dict[str, Any]:
    """Avoid scientific notation (e.g. 9E-3) for small USD amounts in singleValue widgets."""
    return {
        "singleValueFullPrecision": True,
        "setPeriodToTimeRange": True,
    }


def _runtime_cpu_cost_summary_metrics(agent_runtime_arn: str) -> list[Any]:
    return [
        [
            {
                "expression": _round_cost_expression(
                    f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}"
                ),
                "label": "CPU",
                "id": "e1",
            }
        ],
        _agentcore_resource_metric(
            "CPUUsed-vCPUHours", agent_runtime_arn, id="m1", visible=False
        ),
    ]


def _runtime_memory_cost_summary_metrics(agent_runtime_arn: str) -> list[Any]:
    return [
        [
            {
                "expression": _round_cost_expression(
                    f"m1 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}"
                ),
                "label": "Memory",
                "id": "e1",
            }
        ],
        _agentcore_resource_metric(
            "MemoryUsed-GBHours", agent_runtime_arn, id="m1", visible=False
        ),
    ]


def _estimated_cost_component_metrics() -> list[list[dict[str, Any]]]:
    """Stacked cost components; avoids adding missing TimeSeries in one expression."""
    return [
        [
            {
                "expression": f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}",
                "label": "Runtime CPU",
                "id": "e1",
            }
        ],
        [
            {
                "expression": f"m2 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}",
                "label": "Runtime Memory",
                "id": "e2",
            }
        ],
        [{"expression": "m3", "label": "Model", "id": "e3"}],
    ]


def _estimated_total_cost_expression() -> str:
    """Single-value total cost; IF() handles metrics with no data yet."""
    return (
        f"IF(m1, m1, 0) * {RUNTIME_CPU_COST_PER_VCPU_HOUR} + "
        f"IF(m2, m2, 0) * {RUNTIME_MEMORY_COST_PER_GB_HOUR} + "
        f"IF(m3, m3, 0)"
    )


def _bedrock_search_metric(
    metric_name: str,
    stat: str,
    period: int,
    metric_id: str = "e1",
) -> list[list[dict[str, Any]]]:
    """Build a CloudWatch SEARCH metric query for AWS/Bedrock ModelId metrics."""
    return [
        [
            {
                "expression": (
                    f"SEARCH('{{{BEDROCK_NAMESPACE},ModelId}} "
                    f'MetricName=\"{metric_name}\"\', '
                    f"'{stat}', {period})"
                ),
                "id": metric_id,
            }
        ]
    ]


def build_bedrock_usage_dashboard_body(region: str) -> str:
    """Build Bedrock-Usage-Dashboard JSON body with dynamic model discovery."""
    widgets: list[dict[str, Any]] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": 2,
            "properties": {
                "markdown": (
                    f"# {BEDROCK_USAGE_DASHBOARD_NAME}\n"
                    f"**Region:** `{region}` | **Namespace:** `{BEDROCK_NAMESPACE}`\n\n"
                    "모델 ID는 SEARCH 표현식으로 자동 탐색합니다."
                ),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 입력 토큰 (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "setPeriodToTimeRange": True,
                "metrics": _bedrock_search_metric("InputTokenCount", "Sum", 86400),
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 출력 토큰 (24h)",
                "view": "pie",
                "region": region,
                "period": 86400,
                "setPeriodToTimeRange": True,
                "metrics": _bedrock_search_metric("OutputTokenCount", "Sum", 86400),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 8,
            "width": 24,
            "height": 6,
            "properties": {
                "title": "모델별 API 호출 횟수",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "metrics": _bedrock_search_metric("Invocations", "Sum", 3600),
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 14,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 지연 시간 (Average ms)",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "period": 3600,
                "metrics": _bedrock_search_metric("InvocationLatency", "Average", 3600),
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 14,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "모델별 오류 (Client + Server)",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "period": 3600,
                "metrics": [
                    *_bedrock_search_metric("InvocationClientErrors", "Sum", 3600, "e1"),
                    *_bedrock_search_metric("InvocationServerErrors", "Sum", 3600, "e2"),
                ],
            },
        },
    ]
    return json.dumps({"widgets": widgets})


def _estimated_cost_pie_metrics(
    agent_runtime_arn: str,
    project_name: str,
    period: int = 86400,
) -> list[Any]:
    """Pie-chart cost slices: Model / Runtime CPU / Runtime Memory."""
    return [
        [
            {
                "expression": _round_cost_expression(
                    f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}"
                ),
                "label": "Runtime CPU",
                "id": "e1",
            }
        ],
        [
            {
                "expression": _round_cost_expression(
                    f"m2 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}"
                ),
                "label": "Runtime Memory",
                "id": "e2",
            }
        ],
        [{"expression": "m3", "label": "Model", "id": "e3"}],
        _agentcore_resource_metric(
            "CPUUsed-vCPUHours", agent_runtime_arn, id="m1", visible=False
        ),
        _agentcore_resource_metric(
            "MemoryUsed-GBHours", agent_runtime_arn, id="m2", visible=False
        ),
        _custom_project_metric(
            "EstimatedModelCostUSD",
            project_name,
            period=period,
            id="m3",
            visible=False,
        ),
    ]


# Golden-ratio grid (φ ≈ 0.618) for asymmetric layouts on a 24-column dashboard.
_DASHBOARD_GRID = 24
_DASHBOARD_PHI_WIDE = 15
_DASHBOARD_PHI_NARROW = 9


def _dashboard_text_widget(
    x: int, y: int, width: int, height: int, markdown: str
) -> dict[str, Any]:
    return {
        "type": "text",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "properties": {"markdown": markdown},
    }


def _dashboard_metric_widget(
    x: int, y: int, width: int, height: int, properties: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "metric",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "properties": properties,
    }


def _dashboard_section_title(x: int, y: int, width: int, title: str, hint: str = "") -> dict[str, Any]:
    markdown = f"### {title}"
    if hint:
        markdown += f"\n{hint}"
    return _dashboard_text_widget(x, y, width, 1, markdown)


def _dashboard_kpi_widget(
    x: int,
    y: int,
    width: int,
    title: str,
    region: str,
    metrics: list[Any],
    *,
    period: int = 86400,
    cost: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "title": title,
        "view": "singleValue",
        "region": region,
        "period": period,
        "stat": "Sum",
        "sparkline": True,
        "setPeriodToTimeRange": True,
        "metrics": metrics,
    }
    if cost:
        props["yAxis"] = {"left": {"label": "USD", "showUnits": False}}
        props.update(_summary_cost_widget_options())
    return _dashboard_metric_widget(x, y, width, 5, props)


def build_dashboard_body(
    project_name: str,
    agent_runtime_arn: str,
    region: str,
) -> str:
    """Build CloudWatch dashboard JSON body."""
    runtime_id = agent_runtime_arn.rsplit("/", 1)[-1] if agent_runtime_arn else "*"
    dash_name = dashboard_name(project_name)

    def invoke(metric_name: str, **options: Any) -> list[Any]:
        return _agentcore_invoke_metric(metric_name, agent_runtime_arn, **options)

    def resource(metric_name: str, **options: Any) -> list[Any]:
        return _agentcore_resource_metric(metric_name, agent_runtime_arn, **options)

    def custom(metric_name: str, period: int = 300, **options: Any) -> list[Any]:
        return _custom_project_metric(metric_name, project_name, period=period, **options)

    widgets: list[dict[str, Any]] = []
    y = 0

    widgets.append(
        _dashboard_text_widget(
            0,
            y,
            _DASHBOARD_GRID,
            3,
            (
                f"# 🚀 {dash_name}\n"
                f"**Region** `{region}` · **Runtime** `{runtime_id}` · "
                f"**φ-layout** `{_DASHBOARD_PHI_WIDE}:{_DASHBOARD_PHI_NARROW}`\n\n"
                "AgentCore Harness **토큰 · 비용 · 성능** 통합 모니터링. "
                "모델 비용은 커스텀 메트릭, CPU/메모리 비용은 AgentCore vended 메트릭 기반 **추정치**입니다."
            ),
        )
    )
    y += 3

    kpi_specs: list[tuple[int, str, list[Any], bool]] = [
        (0, "🪙 Total Tokens (24h)", [custom("TotalTokens", period=86400)], False),
        (
            4,
            "💵 Model Cost (24h)",
            [
                [{"expression": _round_cost_expression("m1"), "id": "e1"}],
                _custom_project_metric(
                    "EstimatedModelCostUSD",
                    project_name,
                    period=86400,
                    id="m1",
                    visible=False,
                ),
            ],
            True,
        ),
        (8, "⚡ CPU Cost (24h)", _runtime_cpu_cost_summary_metrics(agent_runtime_arn), True),
        (12, "🧠 Memory Cost (24h)", _runtime_memory_cost_summary_metrics(agent_runtime_arn), True),
        (16, "📡 Invocations (24h)", [invoke("Invocations")], False),
        (
            20,
            "💰 Total Cost (24h)",
            [
                [
                    {
                        "expression": _round_cost_expression(
                            _estimated_total_cost_expression()
                        ),
                        "label": "Total",
                        "id": "e1",
                    }
                ],
                *_estimated_cost_source_metrics(
                    agent_runtime_arn, project_name, period=86400
                ),
            ],
            True,
        ),
    ]
    for x, title, metrics, is_cost in kpi_specs:
        widgets.append(_dashboard_kpi_widget(x, y, 4, title, region, metrics, cost=is_cost))
    y += 5

    widgets.append(
        _dashboard_section_title(
            0,
            y,
            _DASHBOARD_GRID,
            "📊 Visual Analytics",
            "24시간 기준 Pie · Bar · Gauge 차트",
        )
    )
    y += 1

    pie_base = {
        "region": region,
        "period": 86400,
        "setPeriodToTimeRange": True,
        "view": "pie",
    }
    widgets.extend(
        [
            _dashboard_metric_widget(
                0,
                y,
                8,
                8,
                {
                    **pie_base,
                    "title": "🥧 Tokens by Model",
                    "labels": {"visible": True},
                    "legend": {"position": "bottom"},
                    "metrics": _tokens_by_model_pie_metrics(
                        project_name, 86400, region=region
                    ),
                },
            ),
            _dashboard_metric_widget(
                8,
                y,
                8,
                8,
                {
                    **pie_base,
                    "title": "🥧 Cost Mix (Model / CPU / Memory)",
                    "metrics": _estimated_cost_pie_metrics(
                        agent_runtime_arn, project_name, period=86400
                    ),
                },
            ),
            _dashboard_metric_widget(
                16,
                y,
                8,
                8,
                {
                    **pie_base,
                    "title": "🥧 Input vs Output Tokens",
                    "metrics": [
                        [
                            {
                                "expression": (
                                    f"SUM({_custom_metric_search_expression('InputTokens', project_name, 86400)})"
                                ),
                                "label": "Input",
                                "id": "e1",
                            }
                        ],
                        [
                            {
                                "expression": (
                                    f"SUM({_custom_metric_search_expression('OutputTokens', project_name, 86400)})"
                                ),
                                "label": "Output",
                                "id": "e2",
                            }
                        ],
                    ],
                },
            ),
        ]
    )
    y += 8

    widgets.append(
        _dashboard_section_title(
            0,
            y,
            _DASHBOARD_GRID,
            "💾 Prompt Cache",
            "cache_read · cache_creation · hit ratio (LLM 호출 기준)",
        )
    )
    y += 1

    cache_kpi_specs: list[tuple[int, str, list[Any], str | None]] = [
        (
            0,
            "📖 Cache Read (24h)",
            [custom("CacheReadTokens", period=86400)],
            None,
        ),
        (
            6,
            "✍️ Cache Write (24h)",
            [custom("CacheCreationTokens", period=86400)],
            None,
        ),
        (
            12,
            "🎯 Hit Ratio (24h)",
            [
                [
                    {
                        "expression": _round_expression(
                            "IF((IF(m1, m1, 0) + IF(m2, m2, 0) + IF(m3, m3, 0)) > 0, "
                            "100 * IF(m1, m1, 0) / (IF(m1, m1, 0) + IF(m2, m2, 0) + IF(m3, m3, 0)), 0)",
                            decimals=2,
                        ),
                        "label": "Hit %",
                        "id": "e1",
                    }
                ],
                _custom_project_metric(
                    "CacheReadTokens",
                    project_name,
                    period=86400,
                    id="m1",
                    visible=False,
                ),
                _custom_project_metric(
                    "CacheCreationTokens",
                    project_name,
                    period=86400,
                    id="m2",
                    visible=False,
                ),
                _custom_project_metric(
                    "UncachedInputTokens",
                    project_name,
                    period=86400,
                    id="m3",
                    visible=False,
                ),
            ],
            "Percent",
        ),
        (
            18,
            "📥 Uncached Input (24h)",
            [custom("UncachedInputTokens", period=86400)],
            None,
        ),
    ]
    for x, title, metrics, y_label in cache_kpi_specs:
        props: dict[str, Any] = {
            "title": title,
            "region": region,
            "view": "singleValue",
            "period": 86400,
            "stat": "Sum",
            "setPeriodToTimeRange": True,
            "metrics": metrics,
        }
        if y_label == "Percent":
            props["yAxis"] = {
                "left": {"min": 0, "max": 100, "label": "%", "showUnits": False}
            }
        widgets.append(_dashboard_metric_widget(x, y, 6, 5, props))
    y += 5

    widgets.extend(
        [
            _dashboard_metric_widget(
                0,
                y,
                8,
                8,
                {
                    **pie_base,
                    "title": "🥧 Input Mix (Uncached / Cache Write / Cache Read)",
                    "metrics": [
                        [
                            {
                                "expression": (
                                    f"SUM({_custom_metric_search_expression('UncachedInputTokens', project_name, 86400)})"
                                ),
                                "label": "Uncached",
                                "id": "e1",
                            }
                        ],
                        [
                            {
                                "expression": (
                                    f"SUM({_custom_metric_search_expression('CacheCreationTokens', project_name, 86400)})"
                                ),
                                "label": "Cache Write",
                                "id": "e2",
                            }
                        ],
                        [
                            {
                                "expression": (
                                    f"SUM({_custom_metric_search_expression('CacheReadTokens', project_name, 86400)})"
                                ),
                                "label": "Cache Read",
                                "id": "e3",
                            }
                        ],
                    ],
                },
            ),
            _dashboard_metric_widget(
                8,
                y,
                16,
                8,
                {
                    "title": "📈 Prompt Cache Tokens (stacked)",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        custom("UncachedInputTokens", label="Uncached"),
                        custom("CacheCreationTokens", label="Cache Write"),
                        custom("CacheReadTokens", label="Cache Read"),
                    ],
                },
            ),
            _dashboard_metric_widget(
                0,
                y + 8,
                _DASHBOARD_PHI_WIDE,
                7,
                {
                    "title": "🎯 Cache Hit Ratio by Model",
                    "view": "timeSeries",
                    "region": region,
                    "period": 300,
                    "yAxis": {"left": {"min": 0, "max": 100, "label": "%", "showUnits": False}},
                    "metrics": _custom_model_metric_query(
                        "CacheHitRatio", project_name, 300, stat="Average"
                    ),
                },
            ),
            _dashboard_metric_widget(
                _DASHBOARD_PHI_WIDE,
                y + 8,
                _DASHBOARD_PHI_NARROW,
                7,
                {
                    "title": "📖 Cache Read by Model",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "metrics": _custom_model_metric_query(
                        "CacheReadTokens", project_name, 300
                    ),
                },
            ),
        ]
    )
    y += 15

    widgets.append(
        _dashboard_section_title(
            0,
            y,
            _DASHBOARD_GRID,
            "🩺 Runtime Health",
            "Gauge · Bar · Stacked Area",
        )
    )
    y += 1

    widgets.extend(
        [
            _dashboard_metric_widget(
                0,
                y,
                6,
                7,
                {
                    "title": "⏱️ Latency p99 (ms)",
                    "view": "gauge",
                    "region": region,
                    "period": 300,
                    "stat": "p99",
                    "metrics": [invoke("Latency")],
                    "yAxis": {"left": {"min": 0, "max": LATENCY_GAUGE_MAX_MS}},
                    "annotations": {
                        "horizontal": [
                            {"color": "#2ca02c", "value": 0},
                            {"color": "#ff9900", "value": LATENCY_GAUGE_WARN_MS},
                            {"color": "#d62728", "value": LATENCY_GAUGE_CRITICAL_MS},
                        ]
                    },
                },
            ),
            _dashboard_metric_widget(
                6,
                y,
                6,
                7,
                {
                    "title": "👥 Active Sessions",
                    "view": "gauge",
                    "region": region,
                    "period": 300,
                    "stat": "Average",
                    "metrics": [
                        [
                            AGENTCORE_NAMESPACE,
                            "ActiveSessionCount",
                            "Service",
                            AGENTCORE_SERVICE,
                        ]
                    ],
                    "yAxis": {"left": {"min": 0, "max": ACTIVE_SESSIONS_GAUGE_MAX}},
                    "annotations": {
                        "horizontal": [
                            {"color": "#2ca02c", "value": 0},
                            {"color": "#ff9900", "value": ACTIVE_SESSIONS_GAUGE_WARN},
                            {
                                "color": "#d62728",
                                "value": ACTIVE_SESSIONS_GAUGE_CRITICAL,
                            },
                        ]
                    },
                },
            ),
            _dashboard_metric_widget(
                12,
                y,
                12,
                7,
                {
                    "title": "📊 Errors & Throttles",
                    "view": "bar",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        invoke("SystemErrors", label="System Errors"),
                        invoke("UserErrors", label="User Errors"),
                        invoke("Throttles", label="Throttles"),
                    ],
                },
            ),
            _dashboard_metric_widget(
                0,
                y + 7,
                _DASHBOARD_PHI_WIDE,
                7,
                {
                    "title": "📈 Runtime Invocations (stacked area)",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        invoke("Invocations", label="AgentCore Invocations"),
                        custom("LLMInvocations", label="LLM Calls"),
                    ],
                },
            ),
            _dashboard_metric_widget(
                _DASHBOARD_PHI_WIDE,
                y + 7,
                _DASHBOARD_PHI_NARROW,
                7,
                {
                    "title": "🔥 Token Throughput",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        custom("InputTokens", label="Input"),
                        custom("OutputTokens", label="Output"),
                    ],
                },
            ),
        ]
    )
    y += 14

    widgets.append(
        _dashboard_section_title(
            0,
            y,
            _DASHBOARD_GRID,
            "🧩 Model & Resources",
            "φ 비율 시계열 · 비용 추이",
        )
    )
    y += 1

    widgets.extend(
        [
            _dashboard_metric_widget(
                0,
                y,
                _DASHBOARD_PHI_WIDE,
                7,
                {
                    "title": "🤖 Total Tokens by Model",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "metrics": _custom_model_metric_query(
                        "TotalTokens", project_name, 300
                    ),
                },
            ),
            _dashboard_metric_widget(
                _DASHBOARD_PHI_WIDE,
                y,
                _DASHBOARD_PHI_NARROW,
                7,
                {
                    "title": "🖥️ Runtime Resources",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        resource("CPUUsed-vCPUHours", label="CPU vCPU-Hours"),
                        resource("MemoryUsed-GBHours", label="Memory GB-Hours"),
                    ],
                },
            ),
            _dashboard_metric_widget(
                0,
                y + 7,
                8,
                7,
                {
                    "title": "💵 Model Cost Trend",
                    "view": "bar",
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "yAxis": {"left": {"label": "USD", "showUnits": False}},
                    "metrics": [custom("EstimatedModelCostUSD")],
                },
            ),
            _dashboard_metric_widget(
                8,
                y + 7,
                8,
                7,
                {
                    "title": "⚡ CPU Cost Trend",
                    "view": "bar",
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "yAxis": {"left": {"label": "USD", "showUnits": False}},
                    "metrics": [
                        [
                            {
                                "expression": _round_cost_expression(
                                    f"m1 * {RUNTIME_CPU_COST_PER_VCPU_HOUR}"
                                ),
                                "label": "CPU Cost",
                                "id": "e1",
                            }
                        ],
                        resource("CPUUsed-vCPUHours", id="m1", visible=False),
                    ],
                },
            ),
            _dashboard_metric_widget(
                16,
                y + 7,
                8,
                7,
                {
                    "title": "🧠 Memory Cost Trend",
                    "view": "bar",
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "yAxis": {"left": {"label": "USD", "showUnits": False}},
                    "metrics": [
                        [
                            {
                                "expression": _round_cost_expression(
                                    f"m1 * {RUNTIME_MEMORY_COST_PER_GB_HOUR}"
                                ),
                                "label": "Memory Cost",
                                "id": "e1",
                            }
                        ],
                        resource("MemoryUsed-GBHours", id="m1", visible=False),
                    ],
                },
            ),
            _dashboard_metric_widget(
                0,
                y + 14,
                _DASHBOARD_GRID,
                8,
                {
                    "title": "💎 Total Estimated Cost — Model + CPU + Memory (stacked)",
                    "view": "timeSeries",
                    "stacked": True,
                    "region": region,
                    "period": 300,
                    "stat": "Sum",
                    "yAxis": {"left": {"label": "USD", "showUnits": False}},
                    "metrics": [
                        *_estimated_cost_component_metrics(),
                        *_estimated_cost_source_metrics(agent_runtime_arn, project_name),
                    ],
                },
            ),
        ]
    )

    return json.dumps({"widgets": widgets})


def create_bedrock_usage_dashboard(region: str) -> str | None:
    """Create or update the Bedrock usage dashboard. Returns dashboard name."""
    name = BEDROCK_USAGE_DASHBOARD_NAME
    body = build_bedrock_usage_dashboard_body(region)

    try:
        client = boto3.client("cloudwatch", region_name=region)
        client.put_dashboard(DashboardName=name, DashboardBody=body)
        url = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{name}"
        )
        print(f"✓ Bedrock usage dashboard created: {name}")
        print(f"  URL: {url}")
        return name
    except Exception as exc:
        print(f"Error creating Bedrock usage dashboard: {exc}")
        return None


def create_cloudwatch_dashboard(
    project_name: str,
    harness_or_runtime_arn: str,
    region: str,
    *,
    runtime_arn: str | None = None,
) -> str | None:
    """Create or update the CloudWatch monitoring dashboard. Returns dashboard name.

    ``harness_or_runtime_arn`` may be the managed Harness ARN; vended widgets bind to
    the backing Runtime ARN (resolved via GetHarness / config / explicit runtime_arn).
    """
    if not harness_or_runtime_arn and not runtime_arn:
        print("Warning: harness/runtime ARN missing; skipping CloudWatch dashboard creation")
        return None

    try:
        resolved_runtime_arn = resolve_harness_runtime_arn(
            harness_or_runtime_arn or runtime_arn or "",
            region,
            runtime_arn=runtime_arn,
        )
    except Exception as exc:
        print(f"Error resolving Harness runtime ARN for dashboard: {exc}")
        return None

    name = dashboard_name(project_name)
    body = build_dashboard_body(project_name, resolved_runtime_arn, region)

    try:
        client = boto3.client("cloudwatch", region_name=region)
        client.put_dashboard(DashboardName=name, DashboardBody=body)
        url = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{name}"
        )
        print(f"✓ CloudWatch dashboard created: {name}")
        print(f"  Runtime ARN: {resolved_runtime_arn}")
        print(f"  URL: {url}")
        return name
    except Exception as exc:
        print(f"Error creating CloudWatch dashboard: {exc}")
        return None
