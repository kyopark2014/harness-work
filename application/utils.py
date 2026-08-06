import logging
import sys
import json
import boto3
import os

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
favorite_tools_path = os.path.join(script_dir, "favorite_tools.json")


def load_config():
    config = None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = {}
        config["projectName"] = "agentcore"

        session = boto3.Session()
        bedrock_region = session.region_name
        config["region"] = bedrock_region

        sts = boto3.client("sts")
        accountId = sts.get_caller_identity()["Account"]
        config["accountId"] = accountId

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    return config


def load_favorite_tools() -> dict[str, list[str]]:
    try:
        with open(favorite_tools_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    favorites: dict[str, list[str]] = {}
    for key in ("MCP", "SKILL"):
        values = data.get(key, [])
        if isinstance(values, list):
            favorites[key] = [v for v in values if isinstance(v, str) and v.strip()]
        else:
            favorites[key] = []
    return favorites


def save_favorite_tools(
    *, skills: list[str] | None = None, mcp_servers: list[str] | None = None
) -> dict[str, list[str]]:
    """Persist favorite tool defaults in favorite_tools.json."""
    favorites = load_favorite_tools()
    if skills is not None:
        favorites["SKILL"] = [v for v in skills if isinstance(v, str) and v.strip()]
    if mcp_servers is not None:
        favorites["MCP"] = [v for v in mcp_servers if isinstance(v, str) and v.strip()]

    with open(favorite_tools_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, ensure_ascii=False, indent=2)
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    """Return initial skill/MCP defaults from favorite_tools.json."""
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    return default_skills, default_mcp_servers


config = load_config()

bedrock_region = config["region"]
projectName = config["projectName"]
accountId = config["accountId"]

