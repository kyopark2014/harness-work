"""App configuration endpoints for the Harness UI."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

try:
    from application import mcp_config, skill, utils
    from application.api.routes_auth import get_optional_user_id
except ImportError:
    import mcp_config  # type: ignore
    import skill  # type: ignore
    import utils  # type: ignore
    from routes_auth import get_optional_user_id  # type: ignore

logger = logging.getLogger("routes_config")

router = APIRouter(prefix="/api/config", tags=["config"])

MODELS = [
    "Claude 5.0 Sonnet",
    "Claude 5.0 Opus",
    "Claude 4.6 Sonnet",
    "Claude Fable 5",
    "Claude 4.7 Opus",
    "Claude 4.6 Opus",
    "Claude 4.5 Haiku",
    "Claude 4.5 Sonnet",
    "Claude 4.5 Opus",
    "OpenAI GPT 5.4",
    "OpenAI GPT 5.5",
    "OpenAI GPT 5.6 Sol",
    "OpenAI GPT 5.6 Terra",
    "OpenAI GPT 5.6 Luna",
    "OpenAI OSS 120B",
    "OpenAI OSS 20B",
    "Nova 2 Lite",
    "Nova Premier",
    "Nova Pro",
    "Nova Lite",
    "Nova Micro",
]

DEFAULT_MODEL = "Claude 4.6 Sonnet"


class DefaultsPatch(BaseModel):
    default_skills: list[str] | None = None
    default_mcp_servers: list[str] | None = None


def _skill_options(user_id: str | None = None) -> list[str]:
    if skill.skill_managers.get("base") is None:
        skill.register_plugin_skills("base")
    return [s["name"] for s in skill.available_skill_info("base", user_id=user_id)]


@router.get("")
def get_config(request: Request):
    user_id = get_optional_user_id(request)  # cookie presence is optional for config
    skill_options = _skill_options(user_id)
    mcp_options = list(mcp_config.MCP_OPTIONS)
    default_skills, default_mcp = utils.get_initial_tool_defaults()
    default_skills = [s for s in default_skills if s in skill_options]
    default_mcp = [m for m in default_mcp if m in mcp_options]
    config = utils.load_config()
    return {
        "projectName": config.get("projectName", "agentcore"),
        "is_admin": False,
        "skills": skill_options,
        "mcp_servers": mcp_options,
        "models": MODELS,
        "gateway_models": [],
        "default_model": DEFAULT_MODEL,
        "default_gateway_model": DEFAULT_MODEL,
        "default_skills": default_skills,
        "default_mcp_servers": default_mcp,
        "llm_gateway_configured": False,
    }


@router.patch("/defaults")
def patch_defaults(body: DefaultsPatch):
    utils.save_favorite_tools(
        skills=body.default_skills,
        mcp_servers=body.default_mcp_servers,
    )
    return {"ok": True}
