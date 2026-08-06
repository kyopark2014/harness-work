"""Minimal InvokeHarness smoke test using application/config.json."""

import json
import os
import sys

import boto3
from botocore.config import Config

working_dir = os.path.dirname(os.path.abspath(__file__))
_config_path = os.path.join(working_dir, "application", "config.json")
try:
    with open(_config_path, encoding="utf-8") as f:
        _cfg = json.load(f)
except FileNotFoundError:
    raise SystemExit(f"Missing {_config_path}. Run installer.py first.") from None

bedrock_region = _cfg.get("region", "us-west-2")
HARNESS_ARN = _cfg.get("HARNESS_ARN")
if not HARNESS_ARN:
    raise SystemExit(
        "HARNESS_ARN is missing in application/config.json. Run installer.py first."
    )

boto_config = Config(
    read_timeout=300,
    connect_timeout=60,
    retries={"max_attempts": 0},
)
runtime = boto3.client(
    "bedrock-agentcore",
    region_name=bedrock_region,
    config=boto_config,
)

# runtimeSessionId: 최소 33자 권장. 동일 ID 재사용 → 대화 이어가기
SESSION_ID = os.environ.get(
    "RUNTIME_SESSION_ID",
    "1234abcd-12ab-34cd-56ef-1234567890ab",
)
PROMPT = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "AWS Document를 이용하여 AgentCore Harness에 대해 조사하세요."
)

print(f"region={bedrock_region}")
print(f"harnessArn={HARNESS_ARN}")
print(f"runtimeSessionId={SESSION_ID}")
print(f"prompt={PROMPT}")
print("---", flush=True)

response = runtime.invoke_harness(
    harnessArn=HARNESS_ARN,
    runtimeSessionId=SESSION_ID,
    actorId="user-alice",  # 사용자별 메모리 격리 (선택)
    messages=[
        {
            "role": "user",
            "content": [{"text": PROMPT}],
        }
    ],
)

stream = response.get("stream")
if stream is None:
    raise SystemExit(f"Empty Harness response: {response}")

# 스트리밍 응답 처리
for event in stream:
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            print(delta["text"], end="", flush=True)
    elif "messageStop" in event:
        print(f"\n\n[Stop reason: {event['messageStop'].get('stopReason')}]")
    elif "metadata" in event:
        usage = event["metadata"].get("usage", {})
        print(
            f"\n[Tokens - input: {usage.get('inputTokens')}, "
            f"output: {usage.get('outputTokens')}]"
        )
    elif "runtimeClientError" in event:
        print(f"\n[Error]: {event['runtimeClientError'].get('message')}")
    else:
        # toolUse / toolResult 등 — 키만 표시
        keys = [k for k in event.keys() if not k.startswith("_")]
        if keys:
            print(f"\n[{', '.join(keys)}]", flush=True)

print(flush=True)
