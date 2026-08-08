"""Bedrock Guardrail helpers for Managed Harness (ECS-side apply_guardrail)."""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

try:
    from application import utils
except ImportError:
    import utils

logger = logging.getLogger(__name__)

DEFAULT_BLOCKED_INPUT = (
    "요청이 안전 정책에 의해 차단되었습니다. "
    "성적 표현 또는 프롬프트 공격이 감지되었습니다."
)
DEFAULT_BLOCKED_OUTPUT = "응답이 안전 정책에 의해 차단되었습니다."


def _guardrail_config(enabled: bool = True) -> dict[str, str] | None:
    if not enabled:
        return None
    config = utils.load_config() or {}
    guardrail_id = config.get("guardrail_id")
    if not guardrail_id:
        return None
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": config.get("guardrail_version", "DRAFT"),
    }


def apply_guardrail_text(
    text: str,
    *,
    source: str,
    region: str,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Return (blocked, message). When blocked, message is the guardrail response."""
    guardrail_cfg = _guardrail_config(enabled)
    if not guardrail_cfg or not (text or "").strip():
        return False, text

    try:
        client = boto3.client("bedrock-runtime", region_name=region)
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_cfg["guardrailIdentifier"],
            guardrailVersion=guardrail_cfg["guardrailVersion"],
            source=source,
            content=[{"text": {"text": text}}],
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            logger.info("Guardrail blocked %s content", source.lower())
            for output in response.get("outputs", []):
                text_output = output.get("text", {})
                if isinstance(text_output, dict) and text_output.get("text"):
                    return True, text_output["text"]
                if isinstance(text_output, str) and text_output:
                    return True, text_output
            fallback = (
                DEFAULT_BLOCKED_INPUT if source == "INPUT" else DEFAULT_BLOCKED_OUTPUT
            )
            return True, fallback
    except ClientError as e:
        logger.error("apply_guardrail failed: %s", e)
    except Exception as e:
        logger.error("apply_guardrail failed: %s", e)
    return False, text


def check_input_guardrail(
    text: str, *, region: str, enabled: bool = True
) -> tuple[bool, str]:
    return apply_guardrail_text(text, source="INPUT", region=region, enabled=enabled)


def check_output_guardrail(
    text: str, *, region: str, enabled: bool = True
) -> tuple[bool, str]:
    return apply_guardrail_text(text, source="OUTPUT", region=region, enabled=enabled)
