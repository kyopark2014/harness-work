"""Discover Agent Skills from skills/ (Anthropic Agent Skills spec)."""

import os
import logging
import sys
from dataclasses import dataclass

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("skill")

APPLICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APPLICATION_DIR)
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")

# S3 Files FS prefix for /mnt/workspace → s3://{bucket}/agentcore-sessions/
S3_FILES_SESSION_PREFIX = "agentcore-sessions"


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


def _is_builtin_skill(name: str) -> bool:
    return os.path.isfile(os.path.join(SKILLS_DIR, name, "SKILL.md"))


def _is_user_skill(user_id: str | None, name: str) -> bool:
    """True if skill-creator output exists locally or as SKILL.md on S3."""
    if not user_id or not name:
        return False
    try:
        import utils

        skills_dir = utils.get_user_skills_dir(user_id)
        if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md")):
            return True

        user_segment = utils.sanitize_user_path_segment(user_id)
        bucket = (utils.load_config() or {}).get("s3_bucket") or utils.s3_bucket
        if not user_segment or not bucket:
            return False
        import boto3

        key = (
            f"{S3_FILES_SESSION_PREFIX}/{user_segment}/skills/{name}/SKILL.md"
        )
        s3 = boto3.client("s3", region_name=utils.bedrock_region)
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _parse_skill_md_text(raw: str) -> tuple[dict, str]:
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


def _list_user_skills_from_s3(user_id: str | None) -> list[dict]:
    """List skill-creator skills under s3://{bucket}/agentcore-sessions/{user}/skills/.

    Used when /mnt/workspace (or SESSION_STORAGE_DIR) is not mounted locally.
    Only directories that contain SKILL.md are included (skips *-workspace eval dirs).
    """
    if not user_id:
        return []
    try:
        import boto3
        import utils

        user_segment = utils.sanitize_user_path_segment(user_id)
        if not user_segment:
            return []
        bucket = (utils.load_config() or {}).get("s3_bucket") or utils.s3_bucket
        if not bucket:
            return []

        prefix = f"{S3_FILES_SESSION_PREFIX}/{user_segment}/skills/"
        s3 = boto3.client("s3", region_name=utils.bedrock_region)
        paginator = s3.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for entry in page.get("CommonPrefixes") or []:
                child = (entry.get("Prefix") or "").rstrip("/")
                name = child.rsplit("/", 1)[-1] if child else ""
                if name:
                    names.append(name)

        skills: list[dict] = []
        for name in sorted(names):
            key = f"{prefix}{name}/SKILL.md"
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                raw = obj["Body"].read().decode("utf-8")
            except Exception:
                # No SKILL.md → not a real skill (e.g. system-monitor-workspace)
                continue
            meta, _ = _parse_skill_md_text(raw)
            skill_name = meta.get("name") or name
            skills.append(
                {
                    "name": skill_name,
                    "description": meta.get("description") or "",
                }
            )
            logger.info(f"Skill discovered (s3): {skill_name}")
        return skills
    except Exception as e:
        logger.warning(f"Failed to list user skills from S3 for {user_id}: {e}")
        return []


def _list_user_skills(user_id: str | None) -> list[dict]:
    """Local SESSION_STORAGE mount first, else S3 agentcore-sessions listing."""
    if not user_id:
        return []
    try:
        import utils

        user_skills_dir = utils.get_user_skills_dir(user_id)
        if os.path.isdir(user_skills_dir):
            return [
                {"name": s.name, "description": s.description}
                for s in SkillManager(user_skills_dir).registry.values()
            ]
    except Exception as e:
        logger.warning(f"Failed to discover local user skills for {user_id}: {e}")
    return _list_user_skills_from_s3(user_id)


def available_skill_info(
    plugin_name: str = "base", user_id: str | None = None
) -> list:
    skill_manager = skill_managers.get(plugin_name)
    if skill_manager is None:
        register_plugin_skills(plugin_name)
        skill_manager = skill_managers[plugin_name]

    # Builtin first; overlay per-user skill-creator skills without mutating
    # the shared registry (multi-user safe).
    by_name: dict[str, dict] = {
        s.name: {"name": s.name, "description": s.description}
        for s in skill_manager.registry.values()
    }
    if user_id and plugin_name == "base":
        for info in _list_user_skills(user_id):
            by_name[info["name"]] = info

    return list(by_name.values())


# skill-creator workspace dirs that must not be attached to InvokeHarness.
# Matches package_skill.ROOT_EXCLUDE_DIRS — evals break S3 skill materialization
# when S3 Files leaves zero-byte directory marker objects (Errno 17 File exists).
_HARNESS_SKILL_ROOT_EXCLUDE_DIRS = frozenset({"evals"})

# Clean copies for InvokeHarness (no markers / no evals).
_HARNESS_USER_SKILL_PREFIX = "skills/users"


def _should_attach_skill_object(rel_key: str) -> bool:
    """Return False for S3 Files directory markers and excluded skill subtrees."""
    if not rel_key or rel_key.endswith("/"):
        return False
    parts = rel_key.split("/")
    if parts[0] in _HARNESS_SKILL_ROOT_EXCLUDE_DIRS:
        return False
    return True


def _list_s3_keys(s3, bucket: str, prefix: str) -> list[dict]:
    keys: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            keys.append(obj)
    return keys


def _delete_s3_prefix_markers(s3, bucket: str, prefix: str) -> int:
    """Delete zero-byte directory marker objects under prefix (S3 Files mkdir artifacts)."""
    to_delete = []
    for obj in _list_s3_keys(s3, bucket, prefix):
        key = obj.get("Key") or ""
        size = int(obj.get("Size") or 0)
        if key.endswith("/") and size == 0:
            to_delete.append({"Key": key})
        elif key == prefix and size == 0:
            to_delete.append({"Key": key})
    deleted = 0
    for i in range(0, len(to_delete), 1000):
        chunk = to_delete[i : i + 1000]
        if not chunk:
            continue
        s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
        deleted += len(chunk)
    if deleted:
        logger.info(
            "Removed %d S3 directory marker(s) under s3://%s/%s",
            deleted,
            bucket,
            prefix,
        )
    return deleted


def materialize_user_skill_for_harness(
    user_id: str | None, name: str
) -> str | None:
    """Copy a skill-creator skill to a clean InvokeHarness S3 prefix.

    Source: ``s3://{bucket}/agentcore-sessions/{user}/skills/{name}/``
    Dest:   ``s3://{bucket}/skills/users/{user}/{name}/``

    Skips directory marker keys (``*/``) and root ``evals/`` so Harness extract
    does not hit ``[Errno 17] File exists: .../evals``.
    Returns the destination ``s3://`` URI, or None on failure.
    """
    if not user_id or not name:
        return None
    try:
        import boto3
        import utils

        user_segment = utils.sanitize_user_path_segment(user_id)
        bucket = (utils.load_config() or {}).get("s3_bucket") or utils.s3_bucket
        if not user_segment or not bucket:
            return None

        src_prefix = (
            f"{S3_FILES_SESSION_PREFIX}/{user_segment}/skills/{name}/"
        )
        dst_prefix = f"{_HARNESS_USER_SKILL_PREFIX}/{user_segment}/{name}/"
        s3 = boto3.client("s3", region_name=utils.bedrock_region)

        # Best-effort: drop markers in the live workspace copy too.
        try:
            _delete_s3_prefix_markers(s3, bucket, src_prefix)
        except Exception as e:
            logger.warning("Failed to clean source markers for %s: %s", name, e)

        src_objects = _list_s3_keys(s3, bucket, src_prefix)
        wanted: dict[str, str] = {}  # dst_key -> src_key
        for obj in src_objects:
            src_key = obj.get("Key") or ""
            if not src_key.startswith(src_prefix):
                continue
            rel = src_key[len(src_prefix) :]
            if not _should_attach_skill_object(rel):
                continue
            wanted[dst_prefix + rel] = src_key

        if not any(k.endswith("SKILL.md") for k in wanted):
            logger.warning(
                "No attachable files (missing SKILL.md) for user skill %s",
                name,
            )
            return None

        # Sync: copy wanted files, delete stale dest keys.
        existing_dst = {
            obj["Key"]
            for obj in _list_s3_keys(s3, bucket, dst_prefix)
            if obj.get("Key")
        }
        for dst_key, src_key in wanted.items():
            s3.copy_object(
                Bucket=bucket,
                Key=dst_key,
                CopySource={"Bucket": bucket, "Key": src_key},
            )
        stale = [
            {"Key": key}
            for key in existing_dst
            if key not in wanted
        ]
        for i in range(0, len(stale), 1000):
            chunk = stale[i : i + 1000]
            if chunk:
                s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": chunk, "Quiet": True}
                )

        uri = f"s3://{bucket}/{dst_prefix}"
        logger.info(
            "Materialized user skill %s for harness (%d files) -> %s",
            name,
            len(wanted),
            uri,
        )
        return uri
    except Exception as e:
        logger.warning(
            "Failed to materialize user skill %s for %s: %s", name, user_id, e
        )
        return None


def build_harness_skills(
    skill_list: list[str], user_id: str | None = None
) -> list[dict]:
    """Map selected skill names to InvokeHarness ``skills`` payloads.

    Local ``skills/<name>/`` (including modified Anthropic docx/pptx/pdf/xlsx)
    is uploaded by installer to ``s3://{bucket}/skills/<name>/`` and attached
    via the S3 skill source so runtime uses those copies—not git.

    Skills created by skill-creator are copied to a clean prefix
    ``s3://{bucket}/skills/users/{user_id}/{name}/`` (markers + ``evals/``
    stripped) before attach — raw ``agentcore-sessions/...`` URIs break Harness
    extract when S3 Files directory placeholders are present.
    """
    if not skill_list:
        return []

    try:
        import utils

        s3_bucket = (utils.load_config() or {}).get("s3_bucket") or ""
        user_segment = utils.sanitize_user_path_segment(user_id)
    except Exception:
        s3_bucket = ""
        user_segment = None

    harness_skills = []
    for name in skill_list:
        use_user_skill = bool(user_segment) and (
            _is_user_skill(user_segment, name) or not _is_builtin_skill(name)
        )
        if s3_bucket and use_user_skill:
            uri = materialize_user_skill_for_harness(user_segment, name)
            if not uri:
                # Last resort: raw session URI (may fail if markers remain)
                uri = (
                    f"s3://{s3_bucket}/{S3_FILES_SESSION_PREFIX}/"
                    f"{user_segment}/skills/{name}/"
                )
                logger.warning(
                    "Using raw session skill URI for %s (materialize failed)",
                    name,
                )
            harness_skills.append({"s3": {"uri": uri}})
        elif s3_bucket:
            harness_skills.append(
                {"s3": {"uri": f"s3://{s3_bucket}/skills/{name}/"}}
            )
        elif use_user_skill and user_segment:
            # Fallback: path inside the runtime session mount
            harness_skills.append({"path": f"{user_segment}/skills/{name}"})
        else:
            # Fallback: path inside the runtime working directory
            harness_skills.append({"path": f"skills/{name}"})
    return harness_skills
