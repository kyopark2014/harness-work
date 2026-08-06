"""Discover Agent Skills from skills/ (Anthropic Agent Skills spec)."""

import os
import logging
import sys
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("skill")

APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APPLICATION_DIR)
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")

# Skills published under https://github.com/anthropics/skills
ANTHROPIC_GIT_SKILLS = {"docx", "pptx", "pdf", "xlsx"}
ANTHROPIC_SKILLS_GIT_URL = "https://github.com/anthropics/skills"


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: str


class SkillManager:
    """Discovers and loads Agent Skills following the Anthropic spec."""

    def __init__(self, skills_dir: str = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.registry: dict[str, Skill] = {}
        self._discover(skills_dir)

    def _discover(self, skills_dir: str):
        if not os.path.isdir(skills_dir):
            logger.info(f"skills directory is not found: {skills_dir}")
            return

        for entry in sorted(os.listdir(skills_dir)):
            skill_md = os.path.join(skills_dir, entry, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            try:
                meta, instructions = self._parse_skill_md(skill_md)
                skill = Skill(
                    name=meta.get("name", entry),
                    description=meta.get("description", ""),
                    instructions=instructions,
                    path=os.path.join(skills_dir, entry),
                )
                self.registry[skill.name] = skill
                logger.info(f"Skill discovered: {skill.name}")
            except Exception as e:
                logger.warning(f"Failed to load skill '{entry}': {e}")

    @staticmethod
    def _parse_skill_md(filepath: str) -> tuple[dict, str]:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()

        if not raw.startswith("---"):
            return {}, raw

        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {}, raw

        try:
            import yaml

            frontmatter = yaml.safe_load(parts[1]) or {}
        except Exception:
            frontmatter = {}
            for line in parts[1].splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                frontmatter[key.strip()] = value.strip().strip('"').strip("'")

        return frontmatter, parts[2].strip()


skill_managers: dict[str, SkillManager] = {}


def register_plugin_skills(plugin_name: str = "base"):
    """Register skills from skills/ into SkillManager."""
    skills_dir = SKILLS_DIR if plugin_name == "base" else os.path.join(
        APPLICATION_DIR, "plugins", plugin_name, "skills"
    )
    skill_manager = skill_managers.get(plugin_name)
    if skill_manager is None:
        skill_manager = SkillManager(skills_dir)
        skill_managers[plugin_name] = skill_manager


def available_skill_info(plugin_name: str = "base") -> list:
    skill_manager = skill_managers.get(plugin_name)
    if skill_manager is None:
        register_plugin_skills(plugin_name)
        skill_manager = skill_managers[plugin_name]

    return [
        {"name": s.name, "description": s.description}
        for s in skill_manager.registry.values()
    ]


def build_harness_skills(skill_list: list[str]) -> list[dict]:
    """Map selected skill names to InvokeHarness ``skills`` payloads."""
    if not skill_list:
        return []

    try:
        import utils

        s3_bucket = (utils.load_config() or {}).get("s3_bucket") or ""
    except Exception:
        s3_bucket = ""

    harness_skills = []
    for name in skill_list:
        if name in ANTHROPIC_GIT_SKILLS:
            harness_skills.append(
                {
                    "git": {
                        "url": ANTHROPIC_SKILLS_GIT_URL,
                        "path": f"skills/{name}",
                    }
                }
            )
        elif s3_bucket:
            harness_skills.append(
                {"s3": {"uri": f"s3://{s3_bucket}/skills/{name}/"}}
            )
        else:
            # Fallback: path inside the runtime working directory
            harness_skills.append({"path": f"skills/{name}"})
    return harness_skills
