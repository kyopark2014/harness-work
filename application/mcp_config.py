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
USER_DEFINED_MCP_PATH = os.path.join(WORKING_DIR, "user_defined_mcp.json")

mcp_user_config: dict = {}

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

MCP_OPTIONS = list(HARNESS_MCP_CATALOG.keys()) + ["사용자 설정"]


def load_user_defined_mcp() -> dict:
    try:
        with open(USER_DEFINED_MCP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load user_defined_mcp.json: {e}")
        return {}


def save_user_defined_mcp(data: dict) -> None:
    with open(USER_DEFINED_MCP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _tools_from_user_config(user_config: dict) -> list[dict]:
    """
    Accept either:
    - {"tools": [HarnessTool, ...]}
    - {"mcpServers": {"name": {"url": "https://..."}}}
    """
    if not user_config:
        return []

    if isinstance(user_config.get("tools"), list):
        return [t for t in user_config["tools"] if isinstance(t, dict)]

    tools = []
    mcp_servers = user_config.get("mcpServers") or {}
    for name, cfg in mcp_servers.items():
        if not isinstance(cfg, dict):
            continue
        url = cfg.get("url")
        if not url:
            logger.warning(
                f"Skipping user MCP '{name}': remote Harness MCP requires a url"
            )
            continue
        tool = {
            "type": "remote_mcp",
            "name": str(name).replace(" ", "_")[:64],
            "config": {"remoteMcp": {"url": url}},
        }
        headers = cfg.get("headers")
        if isinstance(headers, dict) and headers:
            tool["config"]["remoteMcp"]["headers"] = headers
        tools.append(tool)
    return tools


def build_harness_tools(mcp_servers: list[str]) -> list[dict]:
    """Build InvokeHarness ``tools`` from selected MCP option labels."""
    tools: list[dict] = []
    seen_names: set[str] = set()

    for server in mcp_servers or []:
        if server == "사용자 설정":
            for tool in _tools_from_user_config(mcp_user_config or load_user_defined_mcp()):
                name = tool.get("name")
                if name and name in seen_names:
                    continue
                if name:
                    seen_names.add(name)
                tools.append(tool)
            continue

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
