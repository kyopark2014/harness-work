"""Map UI MCP selections to AgentCore Harness ``tools`` for InvokeHarness."""

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp-config")

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "config.json")

# UI labels that map onto the shared project AgentCore Gateway
# (KB retrieve + artifact-share Runtime targets).
_GATEWAY_MCP_LABELS = frozenset({"knowledge base", "artifact-share"})

# Always-on MCP servers for every InvokeHarness call and UI defaults.
# share_artifact / retrieve are required by the harness system prompt.
BASE_MCP_SERVERS: tuple[str, ...] = ("knowledge base", "artifact-share")


def merge_base_mcp_servers(mcp_servers: list[str] | None) -> list[str]:
    """Return selected MCP labels with BASE_MCP_SERVERS always included."""
    merged: list[str] = []
    for label in list(BASE_MCP_SERVERS) + list(mcp_servers or []):
        if label and label not in merged:
            merged.append(label)
    return merged


def _load_app_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _project_mcp_gateway_tool() -> dict | None:
    """Build agentcore_gateway tool for the shared project IAM Gateway.

    Harness ``remote_mcp`` cannot SigV4-sign AgentCore Runtime MCP URLs (403).
    Use the project Gateway (AWS_IAM) with GATEWAY_IAM_ROLE outbound to Runtimes.
    One Gateway fronts multiple Runtime MCP targets (knowledge-base, artifact-share).
    """
    cfg = _load_app_config()
    gateway_arn = (
        (cfg.get("agentcore_gateway_arn") or "").strip()
        or (cfg.get("knowledge_base_mcp_gateway_arn") or "").strip()  # legacy
    )
    if not gateway_arn:
        return None
    return {
        "type": "agentcore_gateway",
        "name": "knowledge_base",
        "config": {
            "agentCoreGateway": {
                "gatewayArn": gateway_arn,
                "outboundAuth": {"awsIam": {}},
            }
        },
    }


# Display labels (agent-plugins style) → InvokeHarness tool definitions
HARNESS_MCP_CATALOG: dict[str, dict] = {
    "websearch": {
        "type": "remote_mcp",
        "name": "exa",
        "config": {"remoteMcp": {"url": "https://mcp.exa.ai/mcp"}},
    },
    "aws_documentation": {
        "type": "remote_mcp",
        "name": "aws_knowledge",
        "config": {
            "remoteMcp": {"url": "https://knowledge-mcp.global.api.aws"}
        },
    },
    "knowledge base": {
        # gatewayArn filled at runtime from application/config.json
        "type": "agentcore_gateway",
        "name": "knowledge_base",
        "config": {"agentCoreGateway": {"gatewayArn": ""}},
    },
    "artifact-share": {
        # Same project Gateway as knowledge base (artifact-share Runtime target).
        "type": "agentcore_gateway",
        "name": "knowledge_base",
        "config": {"agentCoreGateway": {"gatewayArn": ""}},
    },
    "browser-use": {
        "type": "agentcore_browser",
        "name": "browser",
        "config": {"agentCoreBrowser": {}},
    },
    "code interpreter": {
        "type": "agentcore_code_interpreter",
        "name": "code",
        "config": {"agentCoreCodeInterpreter": {}},
    },
}

MCP_OPTIONS = list(HARNESS_MCP_CATALOG.keys())


def build_harness_tools(mcp_servers: list[str]) -> list[dict]:
    """Build InvokeHarness ``tools`` from selected MCP option labels.

    ``knowledge base`` and ``artifact-share`` are always included (shared Gateway).
    """
    tools: list[dict] = []
    seen_names: set[str] = set()
    gateway_attached = False

    for server in merge_base_mcp_servers(mcp_servers):
        if server in _GATEWAY_MCP_LABELS:
            if gateway_attached:
                continue
            tool = _project_mcp_gateway_tool()
            if not tool:
                logger.warning(
                    "%s MCP selected but "
                    "agentcore_gateway_arn is missing from config.json "
                    "(re-run installer.py to create the project IAM Gateway)",
                    server,
                )
                continue
            gateway_attached = True
        else:
            tool = HARNESS_MCP_CATALOG.get(server)
            if not tool:
                logger.warning(f"Unknown MCP option for Harness: {server}")
                continue

        name = tool.get("name")
        if name in seen_names:
            continue
        if name:
            seen_names.add(name)
        tools.append(tool)

    return tools
