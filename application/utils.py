import logging
import sys
import json
import traceback
import boto3
import os
from urllib import parse
from contextlib import contextmanager
import re
from urllib.parse import quote

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


def _default_session_storage_dir() -> str:
    """Resolve session/app storage mount.

    - ECS: ``/mnt/app-data`` (S3 Files prefix ``app-data/``) for tasks.db,
      graph, and settings. Skills are loaded via S3 API, not this mount.
    - Harness runtime: ``/mnt/workspace`` (prefix ``agentcore-sessions/``).
    """
    for candidate in ("/mnt/app-data", "/mnt/workspace"):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(script_dir, ".session_storage")


SESSION_STORAGE_DIR = os.environ.get("SESSION_STORAGE_DIR") or _default_session_storage_dir()

# S3 Files FS prefix for Runtime workspace → s3://{bucket}/agentcore-sessions/
S3_FILES_SESSION_PREFIX = "agentcore-sessions"


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
        try:
            from application import mcp_config
        except ImportError:
            import mcp_config  # type: ignore

        favorites["MCP"] = mcp_config.merge_base_mcp_servers(
            [v for v in mcp_servers if isinstance(v, str) and v.strip()]
        )

    with open(favorite_tools_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, ensure_ascii=False, indent=2)
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    """Return initial skill/MCP defaults from favorite_tools.json.

    Always includes base MCP servers (knowledge base).
    """
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    try:
        from application import mcp_config
    except ImportError:
        import mcp_config  # type: ignore

    default_mcp_servers = mcp_config.merge_base_mcp_servers(default_mcp_servers)
    return default_skills, default_mcp_servers


def get_user_tool_defaults(user_id: str | None) -> tuple[list[str], list[str]]:
    """Per-user skill/MCP defaults from settings.json, else favorite_tools.json."""
    fav_skills, fav_mcp = get_initial_tool_defaults()
    settings = load_user_settings(user_id)
    skills = settings.get("skills")
    mcp_servers = settings.get("mcp_servers")
    return (
        list(skills) if isinstance(skills, list) else fav_skills,
        list(mcp_servers) if isinstance(mcp_servers, list) else fav_mcp,
    )


def save_user_tool_defaults(
    user_id: str | None,
    *,
    skills: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, object]:
    """Persist the user's last skill/MCP selection into settings.json."""
    updates: dict[str, object] = {}
    if skills is not None:
        updates["skills"] = skills
    if mcp_servers is not None:
        updates["mcp_servers"] = mcp_servers
    if not updates:
        return load_user_settings(user_id)
    return save_user_settings(user_id, **updates)


config = load_config()

bedrock_region = config["region"]
projectName = config["projectName"]
accountId = config["accountId"]
s3_bucket = config.get("s3_bucket") or (
    f"storage-for-{projectName}-{accountId}-{bedrock_region}"
)
sharing_url = (config.get("sharing_url") or "").rstrip("/")
knowledge_base_id = config.get("knowledge_base_id") or ""
data_source_id = config.get("data_source_id") or ""


def sanitize_user_path_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user S3 folders, or None."""
    if not user_id:
        return None
    raw = str(user_id).strip()
    if raw.startswith("v1.") and raw.count(".") >= 2:
        logger.warning("Refusing signed session token as S3 path segment")
        return None
    if len(raw) > 128:
        logger.warning("Refusing oversized user_id as S3 path segment")
        return None
    segment = (
        raw.replace("/", "_").replace("\\", "_").replace("..", "_")
    )
    return segment or None


def get_user_skills_dir(user_id: str | None) -> str:
    """Logical local path for user skills (Harness runtime mount only).

    Web UI discovers skill-creator skills via S3
    (``agentcore-sessions/{user}/skills/``), not under app-data.
    """
    segment = sanitize_user_path_segment(user_id) or "default"
    # Prefer workspace mount when present (runtime); else app-data is unused
    # for skills listing — callers should use S3.
    root = "/mnt/workspace" if os.path.isdir("/mnt/workspace") else SESSION_STORAGE_DIR
    return os.path.join(root, segment, "skills")


def ensure_user_skills_dir(user_id: str | None) -> str:
    """Create user skills dir under the Harness workspace mount when available."""
    skills_dir = get_user_skills_dir(user_id)
    os.makedirs(skills_dir, exist_ok=True)
    logger.info("user skills dir ready: %s", skills_dir)
    return skills_dir


def get_user_graph_dir(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/graph (does not create)."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph")


def ensure_user_graph_dir(user_id: str | None) -> str:
    """Create session graph workspace: corpus/ + out/.

    Returns the graph root: {SESSION_STORAGE_DIR}/{user_id}/graph
    """
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for graph path; expected a plain user id, "
            "not a signed session cookie"
        )
    graph_dir = os.path.join(SESSION_STORAGE_DIR, segment, "graph")
    for name in ("corpus", "out"):
        os.makedirs(os.path.join(graph_dir, name), exist_ok=True)
    logger.info("user graph dir ready: %s", graph_dir)
    return graph_dir


def user_graph_html_path(user_id: str | None) -> str:
    """Published HTML: {SESSION_STORAGE_DIR}/{user_id}/graph/out/graph.html"""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "graph", "out", "graph.html")


GRAPH_PATTERNS = ("pattern1", "pattern2", "pattern3")
DEFAULT_GRAPH_PATTERN = "pattern1"

_DEFAULT_USER_SETTINGS: dict[str, object] = {
    "knowledge_graph_enabled": True,
    "graph_pattern": DEFAULT_GRAPH_PATTERN,
    "documents_foundation_model_parser_enabled": True,
    "documents_parallel_processing_enabled": True,
}


def normalize_graph_pattern(value: object | None) -> str:
    raw = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    aliases = {
        "pattern1": "pattern1",
        "p1": "pattern1",
        "1": "pattern1",
        "forceatlas": "pattern1",
        "pattern2": "pattern2",
        "p2": "pattern2",
        "2": "pattern2",
        "neo4j": "pattern2",
        "neo4jexplore": "pattern2",
        "pattern3": "pattern3",
        "p3": "pattern3",
        "3": "pattern3",
        "holistic": "pattern3",
        "holisticview": "pattern3",
    }
    return aliases.get(raw, DEFAULT_GRAPH_PATTERN)



def get_user_db_path(user_id: str | None) -> str:
    """Durable per-user tasks/messages DB: {SESSION_STORAGE_DIR}/{user_id}/{user_id}.db."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, f"{segment}.db")


def get_user_settings_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/settings.json (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "settings.json")


def _normalize_string_list(value: object) -> list[str]:
    """Return a cleaned list of non-empty strings (stable order, no duplicates)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def load_user_settings(user_id: str | None) -> dict[str, object]:
    """Load per-user UI/feature settings. Missing file → defaults (KG on).

    ``skills`` / ``mcp_servers`` are omitted until the user has saved them so
    callers can fall back to favorite_tools.json.
    """
    settings = dict(_DEFAULT_USER_SETTINGS)
    path = get_user_settings_path(user_id)
    if not os.path.isfile(path):
        return settings
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "knowledge_graph_enabled" in raw:
                settings["knowledge_graph_enabled"] = bool(raw["knowledge_graph_enabled"])
            if "graph_pattern" in raw:
                settings["graph_pattern"] = normalize_graph_pattern(raw.get("graph_pattern"))
            if "skills" in raw:
                settings["skills"] = _normalize_string_list(raw.get("skills"))
            if "mcp_servers" in raw:
                settings["mcp_servers"] = _normalize_string_list(raw.get("mcp_servers"))
            if "documents_foundation_model_parser_enabled" in raw:
                settings["documents_foundation_model_parser_enabled"] = bool(
                    raw["documents_foundation_model_parser_enabled"]
                )
            if "documents_parallel_processing_enabled" in raw:
                settings["documents_parallel_processing_enabled"] = bool(
                    raw["documents_parallel_processing_enabled"]
                )
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load user settings %s: %s", path, e)
    return settings


def save_user_settings(user_id: str | None, **updates: object) -> dict[str, object]:
    """Merge updates into per-user settings.json and return the full settings."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for settings path; expected a plain user id, "
            "not a signed session cookie"
        )
    user_dir = os.path.join(SESSION_STORAGE_DIR, segment)
    os.makedirs(user_dir, exist_ok=True)
    settings = load_user_settings(user_id)
    for key, value in updates.items():
        if key == "knowledge_graph_enabled":
            settings[key] = bool(value)
        elif key == "graph_pattern":
            settings[key] = normalize_graph_pattern(value)
        elif key == "skills":
            settings[key] = _normalize_string_list(value)
        elif key == "mcp_servers":
            settings[key] = _normalize_string_list(value)
        elif key == "documents_foundation_model_parser_enabled":
            settings[key] = bool(value)
        elif key == "documents_parallel_processing_enabled":
            settings[key] = bool(value)
    path = get_user_settings_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("user settings saved: %s -> %s", path, settings)
    return settings


def is_knowledge_graph_enabled(user_id: str | None) -> bool:
    """True when Knowledge Graph feature is on (default)."""
    return bool(load_user_settings(user_id).get("knowledge_graph_enabled", True))



def is_hybrid_graph_search_enabled() -> bool:
    """True when config.json hybrid_graph_search is enable (embedding vector search)."""
    cfg = load_config() or {}
    raw = str(cfg.get("hybrid_graph_search") or "").strip().lower()
    return raw in {"enable", "enabled", "on", "true", "1", "yes"}


def get_graph_pattern(user_id: str | None) -> str:
    """Selected Knowledge Graph HTML pattern (pattern1|pattern2|pattern3)."""
    return normalize_graph_pattern(
        load_user_settings(user_id).get("graph_pattern", DEFAULT_GRAPH_PATTERN)
    )



def get_contents_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith((".ppt", ".pptx")):
        return "application/vnd.ms-powerpoint"
    if lower.endswith((".doc", ".docx")):
        return "application/msword"
    if lower.endswith((".xls", ".xlsx")):
        return "application/vnd.ms-excel"
    if lower.endswith(".py"):
        return "text/x-python"
    if lower.endswith(".js"):
        return "application/javascript"
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith((".html", ".htm")):
        return "text/html; charset=utf-8"
    if lower.endswith(".json"):
        return "application/json"
    return "no info"


def upload_to_s3(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict | None:
    """Upload a file to S3 under docs/ or images/ and return upload metadata."""
    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        content_type = get_contents_type(file_name)
        prefix = "images" if content_type.startswith("image/") else "docs"
        user_segment = sanitize_user_path_segment(user_id)
        if user_segment:
            s3_key = f"{prefix}/{user_segment}/{file_name}"
            relative_url_path = (
                f"{prefix}/{parse.quote(user_segment)}/{parse.quote(file_name)}"
            )
        else:
            s3_key = f"{prefix}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(file_name)}"

        put_params = {
            "Bucket": s3_bucket,
            "Key": s3_key,
            "Metadata": {"content_type": content_type},
            "Body": file_bytes,
        }
        if content_type != "no info":
            put_params["ContentType"] = content_type
        if content_type == "application/pdf":
            put_params["ContentDisposition"] = "inline"

        s3_client.put_object(**put_params)

        url = None
        if sharing_url:
            url = f"{sharing_url}/{relative_url_path}"

        return {
            "file_name": file_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "url": url,
        }
    except Exception:
        logger.error("Error uploading to S3: %s", traceback.format_exc())
        return None


def get_active_ingestion_job() -> dict | None:
    """Return the in-progress KB ingestion job, if any."""
    if not knowledge_base_id or not data_source_id:
        return None
    try:
        client = boto3.client("bedrock-agent", region_name=bedrock_region)
        response = client.list_ingestion_jobs(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            maxResults=5,
        )
        for job in response.get("ingestionJobSummaries") or []:
            if job.get("status") in ("IN_PROGRESS", "STARTING"):
                return job
        return None
    except Exception:
        logger.error("Error listing ingestion jobs: %s", traceback.format_exc())
        raise


def sync_data_source() -> dict | None:
    """Start a Knowledge Base ingestion job for the configured data source."""
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None
    try:
        client = boto3.client("bedrock-agent", region_name=bedrock_region)
        response = client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        )
        job = response.get("ingestionJob", {})
        return {
            "ingestion_job_id": job.get("ingestionJobId"),
            "status": job.get("status"),
        }
    except Exception:
        logger.error("Error syncing data source: %s", traceback.format_exc())
        return None


def s3_key_from_file_ref(file_ref: str) -> str | None:
    """Extract an S3 object key from a CloudFront/S3 URL or raw key."""
    ref = (file_ref or "").strip()
    if not ref:
        return None
    if ref.startswith("s3://"):
        without = ref[5:]
        parts = without.split("/", 1)
        return parts[1] if len(parts) == 2 else None
    if "://" in ref:
        path = parse.urlparse(ref).path.lstrip("/")
        return parse.unquote(path) if path else None
    if ref.startswith("images/") or ref.startswith("docs/"):
        return ref
    return None


def load_image_bytes_from_ref(file_ref: str) -> tuple[str, bytes]:
    """Load image bytes from S3 given a URL or key. Returns (file_name, bytes)."""
    if not s3_bucket:
        raise ValueError("s3_bucket is not configured")
    s3_key = s3_key_from_file_ref(file_ref)
    if not s3_key:
        raise ValueError(f"Cannot resolve S3 key from ref: {file_ref}")
    file_name = os.path.basename(s3_key)
    s3_client = boto3.client("s3", region_name=bedrock_region)
    logger.info("loading image from s3://%s/%s", s3_bucket, s3_key)
    image_obj = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
    return file_name, image_obj["Body"].read()



# --- Documents helpers (ported from agentic-work) ---

def get_user_artifacts_dir(user_id: str | None) -> str:
    """Logical path for user artifacts (Runtime /mnt/workspace when present)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    root = "/mnt/workspace" if os.path.isdir("/mnt/workspace") else SESSION_STORAGE_DIR
    return os.path.join(root, segment, "artifacts")


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    """Create artifacts dir under Runtime workspace when available; skip on ECS app-data."""
    artifacts_dir = get_user_artifacts_dir(user_id)
    if not os.path.isdir("/mnt/workspace") and os.path.isdir("/mnt/app-data"):
        return artifacts_dir
    os.makedirs(artifacts_dir, exist_ok=True)
    logger.info("user artifacts dir ready: %s", artifacts_dir)
    return artifacts_dir


def get_user_documents_dir(user_id: str | None) -> str:
    """Per-user Documents root: ``{SESSION_STORAGE_DIR}/{user_id}/documents``."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "documents")


def _ensure_documents_on_path() -> str:
    """Put ``harness-work/documents`` on ``sys.path`` so ``doc_list`` is importable."""
    docs_pkg = os.path.join(os.path.dirname(script_dir), "documents")
    if docs_pkg not in sys.path:
        sys.path.insert(0, docs_pkg)
    return docs_pkg


def ensure_user_documents_dir(user_id: str | None) -> str:
    """Create ``{user}/documents``, ``projects/``, ``drawings/``, ``out/``, …."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for documents path; expected a plain user id, "
            "not a signed session cookie"
        )
    docs_dir = os.path.join(SESSION_STORAGE_DIR, segment, "documents")
    for name in (
        "",
        "projects",
        "drawings",
        "out",
        os.path.join("out", "converted"),
        os.path.join("out", "converted", ".pdf_pages"),
    ):
        os.makedirs(os.path.join(docs_dir, name) if name else docs_dir, exist_ok=True)
    try:
        _ensure_documents_on_path()
        from doc_list import (
            DRAWINGS,
            PROJECTS,
            doc_list_path,
            empty_doc_list,
            save_doc_list,
            sync_doc_list_with_filesystem,
        )

        if not doc_list_path(docs_dir, PROJECTS).is_file():
            projects = os.path.join(docs_dir, "projects")
            has_projects = os.path.isdir(projects) and any(
                os.path.isfile(os.path.join(projects, n)) for n in os.listdir(projects)
            )
            if has_projects:
                sync_doc_list_with_filesystem(
                    docs_dir, user_id=segment, registry=PROJECTS
                )
            else:
                save_doc_list(
                    docs_dir, empty_doc_list(user_id=segment), registry=PROJECTS
                )
        if not doc_list_path(docs_dir, DRAWINGS).is_file():
            drawings = os.path.join(docs_dir, "drawings")
            has_drawings = os.path.isdir(drawings) and any(
                os.path.isfile(os.path.join(drawings, n)) for n in os.listdir(drawings)
            )
            if has_drawings:
                sync_doc_list_with_filesystem(
                    docs_dir, user_id=segment, registry=DRAWINGS
                )
            else:
                save_doc_list(
                    docs_dir, empty_doc_list(user_id=segment), registry=DRAWINGS
                )
    except Exception:
        logger.debug("documents doc_list ensure skipped", exc_info=True)
    logger.debug("user documents dir ready: %s", docs_dir)
    return docs_dir


def documents_converted_dir(user_id: str | None = None) -> str:
    return os.path.join(documents_out_dir(user_id), "converted")


def documents_out_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "out")


def documents_projects_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "projects")


def documents_project_list_path(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "project_list.json")


def documents_drawings_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "drawings")


def documents_drawings_list_path(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "drawings_list.json")


def _documents_docs_dest_path(docs_dir: str, filename: str) -> tuple[str, str, str]:
    """Return ``(dest_path, sanitized_name, original_basename)``."""
    original = os.path.basename((filename or "").strip()) or "upload.bin"
    original = original.replace("\x00", "_") or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe = sanitize_documents_filename(original)
    except Exception:
        safe = original.replace(" ", "_")
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in safe)
        while "__" in safe:
            safe = safe.replace("__", "_")
        stem, ext = os.path.splitext(safe)
        safe = f"{stem.strip('._-') or 'document'}{ext.lower()}"
    return os.path.join(docs_dir, safe), safe, original


def save_documents_project_upload(
    filename: str,
    data: bytes,
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """Sanitize filename, write into ``{user}/documents/projects``, update project_list."""
    if data is None or len(data) == 0:
        raise ValueError("저장할 파일이 없습니다.")

    root = ensure_user_documents_dir(user_id)
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    dest, safe_name, original_name = _documents_docs_dest_path(projects, filename)
    overwritten = os.path.isfile(dest)
    with open(dest, "wb") as f:
        f.write(data)

    segment = sanitize_user_path_segment(user_id) or "default"
    try:
        _ensure_documents_on_path()
        from doc_list import PROJECTS, upsert_document

        upsert_document(
            root,
            filename=safe_name,
            source_path=os.path.abspath(dest),
            bytes_size=len(data),
            status="uploaded",
            user_id=segment,
            extra={
                "original_filename": original_name,
                "sanitized": original_name != safe_name,
            },
            registry=PROJECTS,
        )
    except Exception:
        logger.exception("Failed to update documents project_list after upload")

    return {
        "documents_dir": root,
        "projects_dir": projects,
        "docs_dir": projects,
        "raw_dir": projects,
        "saved": {
            "name": safe_name,
            "original_filename": original_name,
            "sanitized": original_name != safe_name,
            "path": dest,
            "bytes": len(data),
            "overwritten": overwritten,
        },
        "count": 1,
        "project_list": documents_project_list_path(user_id),
    }


def save_documents_drawing_upload(
    filename: str,
    data: bytes,
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """Sanitize filename, write into ``{user}/documents/drawings``, update drawings_list."""
    if data is None or len(data) == 0:
        raise ValueError("저장할 파일이 없습니다.")

    root = ensure_user_documents_dir(user_id)
    drawings = os.path.join(root, "drawings")
    os.makedirs(drawings, exist_ok=True)
    dest, safe_name, original_name = _documents_docs_dest_path(drawings, filename)
    overwritten = os.path.isfile(dest)
    with open(dest, "wb") as f:
        f.write(data)

    segment = sanitize_user_path_segment(user_id) or "default"
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, upsert_document

        upsert_document(
            root,
            filename=safe_name,
            source_path=os.path.abspath(dest),
            bytes_size=len(data),
            status="uploaded",
            user_id=segment,
            extra={
                "original_filename": original_name,
                "sanitized": original_name != safe_name,
            },
            registry=DRAWINGS,
        )
    except Exception:
        logger.exception("Failed to update documents drawings_list after upload")

    return {
        "documents_dir": root,
        "drawings_dir": drawings,
        "docs_dir": drawings,
        "raw_dir": drawings,
        "saved": {
            "name": safe_name,
            "original_filename": original_name,
            "sanitized": original_name != safe_name,
            "path": dest,
            "bytes": len(data),
            "overwritten": overwritten,
        },
        "count": 1,
        "drawings_list": documents_drawings_list_path(user_id),
    }


def list_documents_project_files(user_id: str | None = None) -> list[dict[str, object]]:
    projects = documents_projects_dir(user_id)
    if not os.path.isdir(projects):
        return []
    out: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        return []
    for name in names:
        path = os.path.join(projects, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "bytes": size, "mtime": mtime})
    return out


def list_documents_drawing_files(user_id: str | None = None) -> list[dict[str, object]]:
    drawings = documents_drawings_dir(user_id)
    if not os.path.isdir(drawings):
        return []
    out: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(drawings))
    except OSError:
        return []
    for name in names:
        path = os.path.join(drawings, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "bytes": size, "mtime": mtime})
    return out


def is_documents_foundation_model_parser_enabled(user_id: str | None) -> bool:
    return bool(
        load_user_settings(user_id).get(
            "documents_foundation_model_parser_enabled", True
        )
    )


def set_documents_foundation_model_parser_enabled(
    enabled: bool, *, user_id: str | None
) -> bool:
    settings = save_user_settings(
        user_id, documents_foundation_model_parser_enabled=bool(enabled)
    )
    return bool(settings.get("documents_foundation_model_parser_enabled", True))


def is_documents_parallel_processing_enabled(user_id: str | None) -> bool:
    return bool(
        load_user_settings(user_id).get("documents_parallel_processing_enabled", True)
    )


def set_documents_parallel_processing_enabled(
    enabled: bool, *, user_id: str | None
) -> bool:
    settings = save_user_settings(
        user_id, documents_parallel_processing_enabled=bool(enabled)
    )
    return bool(settings.get("documents_parallel_processing_enabled", True))

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def _without_env_proxies():
    """Drop HTTP(S)_PROXY for the block (Cursor agent proxies break local boto3)."""
    saved = {key: os.environ.pop(key, None) for key in _PROXY_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _s3_client_for_presign():
    """S3 client for browser-safe regional, virtual-hosted presigned URLs.

    Global ``*.s3.amazonaws.com`` hosts often 307-redirect to the region
    endpoint; browsers then fail the signed PUT (403/CORS) and our API never
    sees ``/complete``. Prefer virtual-hosted
    ``https://{bucket}.s3.{region}.amazonaws.com/...`` via SigV4 + regional
    endpoint so the browser PUT never follows a TemporaryRedirect.
    """
    from botocore.config import Config

    region = bedrock_region or "us-west-2"
    return boto3.client(
        service_name="s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        ),
    )

def _session_upload_content_type(file_name: str) -> str:
    """Content-Type for session uploads; never returns ``no info``."""
    content_type = get_contents_type(file_name)
    if content_type == "no info":
        return "application/octet-stream"
    return content_type

DOCUMENTS_S3_PREFIX = "session-uploads"
MAX_DOCUMENTS_DOC_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def documents_projects_s3_key(file_name: str, user_id: str | None = None) -> str:
    """Build ``session-uploads/{user}/documents/projects/{file}`` staging key."""
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "upload.bin"
    return f"{DOCUMENTS_S3_PREFIX}/{segment}/documents/projects/{safe_name}"


def generate_documents_projects_presigned_put(
    file_name: str,
    user_id: str | None = None,
    *,
    expires_in: int = 900,
) -> dict | None:
    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    original = os.path.basename(file_name or "").strip() or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(original)
    except Exception:
        safe_name = original.replace(" ", "_")

    s3_key = documents_projects_s3_key(safe_name, user_id=user_id)
    content_type = _session_upload_content_type(safe_name)
    headers = {"Content-Type": content_type}
    params: dict = {
        "Bucket": s3_bucket,
        "Key": s3_key,
        "ContentType": content_type,
    }
    if content_type == "application/pdf":
        params["ContentDisposition"] = "inline"
        headers["Content-Disposition"] = "inline"

    try:
        with _without_env_proxies():
            s3_client = _s3_client_for_presign()
            upload_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=max(60, int(expires_in)),
                HttpMethod="PUT",
            )
        return {
            "file_name": safe_name,
            "original_filename": original,
            "sanitized": original != safe_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "upload_url": upload_url,
            "headers": headers,
            "expires_in": max(60, int(expires_in)),
        }
    except Exception:
        logger.error(
            "Error generating documents projects presign: %s", traceback.format_exc()
        )
        return None


def materialize_documents_projects_from_s3(
    s3_key: str,
    file_name: str,
    user_id: str | None = None,
    *,
    original_filename: str | None = None,
) -> dict | None:
    if not s3_bucket or not s3_key:
        return None

    original = (
        os.path.basename(original_filename or file_name or "").strip()
        or "upload.bin"
    )
    try:
        _ensure_documents_on_path()
        from doc_list import PROJECTS, sanitize_documents_filename, upsert_document

        safe_name = sanitize_documents_filename(file_name or original)
    except Exception:
        safe_name = os.path.basename(file_name or original) or "upload.bin"
        upsert_document = None  # type: ignore[assignment]
        PROJECTS = None  # type: ignore[assignment]

    root = ensure_user_documents_dir(user_id)
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    dest_path = os.path.join(projects, safe_name)
    overwritten = os.path.isfile(dest_path)

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        s3_client.download_file(s3_bucket, s3_key, dest_path)
        size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
        if size <= 0:
            logger.error("Documents project materialize empty: %s", dest_path)
            return None

        segment = sanitize_user_path_segment(user_id) or "default"
        if upsert_document is not None and PROJECTS is not None:
            try:
                upsert_document(
                    root,
                    filename=safe_name,
                    source_path=os.path.abspath(dest_path),
                    bytes_size=size,
                    status="uploaded",
                    user_id=segment,
                    extra={
                        "original_filename": original,
                        "sanitized": original != safe_name,
                        "s3_key": s3_key,
                    },
                    registry=PROJECTS,
                )
            except Exception:
                logger.exception("Failed to update documents project_list after materialize")

        return {
            "documents_dir": root,
            "projects_dir": projects,
            "docs_dir": projects,
            "raw_dir": projects,
            "saved": {
                "name": safe_name,
                "original_filename": original,
                "sanitized": original != safe_name,
                "path": dest_path,
                "bytes": size,
                "overwritten": overwritten,
            },
            "count": 1,
            "s3_key": s3_key,
            "project_list": documents_project_list_path(user_id),
            "content_type": _session_upload_content_type(safe_name),
            "content_length": size,
        }
    except Exception:
        logger.error(
            "Error materializing documents projects key=%s: %s",
            s3_key,
            traceback.format_exc(),
        )
        return None


def documents_drawings_s3_key(file_name: str, user_id: str | None = None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "upload.bin"
    return f"{DOCUMENTS_S3_PREFIX}/{segment}/documents/drawings/{safe_name}"


def generate_documents_drawings_presigned_put(
    file_name: str,
    user_id: str | None = None,
    *,
    expires_in: int = 900,
) -> dict | None:
    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    original = os.path.basename(file_name or "").strip() or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(original)
    except Exception:
        safe_name = original.replace(" ", "_")

    s3_key = documents_drawings_s3_key(safe_name, user_id=user_id)
    content_type = _session_upload_content_type(safe_name)
    headers = {"Content-Type": content_type}
    params: dict = {
        "Bucket": s3_bucket,
        "Key": s3_key,
        "ContentType": content_type,
    }
    if content_type == "application/pdf":
        params["ContentDisposition"] = "inline"
        headers["Content-Disposition"] = "inline"

    try:
        with _without_env_proxies():
            s3_client = _s3_client_for_presign()
            upload_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=max(60, int(expires_in)),
                HttpMethod="PUT",
            )
        return {
            "file_name": safe_name,
            "original_filename": original,
            "sanitized": original != safe_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "upload_url": upload_url,
            "headers": headers,
            "expires_in": max(60, int(expires_in)),
        }
    except Exception:
        logger.error(
            "Error generating documents drawings presign: %s", traceback.format_exc()
        )
        return None


def materialize_documents_drawings_from_s3(
    s3_key: str,
    file_name: str,
    user_id: str | None = None,
    *,
    original_filename: str | None = None,
) -> dict | None:
    if not s3_bucket or not s3_key:
        return None

    original = (
        os.path.basename(original_filename or file_name or "").strip()
        or "upload.bin"
    )
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, sanitize_documents_filename, upsert_document

        safe_name = sanitize_documents_filename(file_name or original)
    except Exception:
        safe_name = os.path.basename(file_name or original) or "upload.bin"
        upsert_document = None  # type: ignore[assignment]
        DRAWINGS = None  # type: ignore[assignment]

    root = ensure_user_documents_dir(user_id)
    drawings = os.path.join(root, "drawings")
    os.makedirs(drawings, exist_ok=True)
    dest_path = os.path.join(drawings, safe_name)
    overwritten = os.path.isfile(dest_path)

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        s3_client.download_file(s3_bucket, s3_key, dest_path)
        size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
        if size <= 0:
            logger.error("Documents drawing materialize empty: %s", dest_path)
            return None

        segment = sanitize_user_path_segment(user_id) or "default"
        if upsert_document is not None and DRAWINGS is not None:
            try:
                upsert_document(
                    root,
                    filename=safe_name,
                    source_path=os.path.abspath(dest_path),
                    bytes_size=size,
                    status="uploaded",
                    user_id=segment,
                    extra={
                        "original_filename": original,
                        "sanitized": original != safe_name,
                        "s3_key": s3_key,
                    },
                    registry=DRAWINGS,
                )
            except Exception:
                logger.exception("Failed to update documents drawings_list after materialize")

        return {
            "documents_dir": root,
            "drawings_dir": drawings,
            "docs_dir": drawings,
            "raw_dir": drawings,
            "saved": {
                "name": safe_name,
                "original_filename": original,
                "sanitized": original != safe_name,
                "path": dest_path,
                "bytes": size,
                "overwritten": overwritten,
            },
            "count": 1,
            "s3_key": s3_key,
            "drawings_list": documents_drawings_list_path(user_id),
            "content_type": _session_upload_content_type(safe_name),
            "content_length": size,
        }
    except Exception:
        logger.error(
            "Error materializing documents drawings key=%s: %s",
            s3_key,
            traceback.format_exc(),
        )
        return None


def documents_project_pdf_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not sharing_url:
        return None
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    segment = sanitize_user_path_segment(user_id) or "default"
    relative = (
        f"{DOCUMENTS_S3_PREFIX}/{parse.quote(segment)}/documents/projects/"
        f"{parse.quote(safe_name)}"
    )
    return f"{sharing_url.rstrip('/')}/{relative}"


def documents_drawing_pdf_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not sharing_url:
        return None
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    segment = sanitize_user_path_segment(user_id) or "default"
    relative = (
        f"{DOCUMENTS_S3_PREFIX}/{parse.quote(segment)}/documents/drawings/"
        f"{parse.quote(safe_name)}"
    )
    return f"{sharing_url.rstrip('/')}/{relative}"


def documents_md_artifacts_s3_key(file_name: str, user_id: str | None = None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    project = (projectName or "default").strip().strip("/") or "default"
    return f"artifacts/{project}/{segment}/md/{safe_name}"


def documents_md_runtime_workspace_s3_key(
    file_name: str, user_id: str | None = None
) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    return f"{S3_FILES_SESSION_PREFIX}/{segment}/artifacts/md/{safe_name}"


def documents_md_artifacts_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not sharing_url:
        return None
    key = documents_md_artifacts_s3_key(file_name, user_id=user_id)
    parts = [parse.quote(p) for p in key.split("/")]
    return f"{sharing_url.rstrip('/')}/{'/'.join(parts)}"


def documents_md_local_artifacts_path(
    file_name: str, user_id: str | None = None
) -> str:
    artifacts = ensure_user_artifacts_dir(user_id)
    md_dir = os.path.join(artifacts, "md")
    os.makedirs(md_dir, exist_ok=True)
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    return os.path.join(md_dir, safe_name)


def _documents_head_s3_object_quiet(s3_key: str) -> dict | None:
    if not s3_bucket or not s3_key:
        return None
    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        response = s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
        return {
            "content_length": int(response.get("ContentLength") or 0),
            "content_type": response.get("ContentType"),
        }
    except Exception:
        return None


def publish_documents_markdown_to_artifacts(
    md_path: str,
    user_id: str | None = None,
    *,
    file_name: str | None = None,
) -> dict | None:
    """Copy markdown to artifacts and upload to S3 for CloudFront + Runtime."""
    from pathlib import Path

    src = Path(md_path)
    if not src.is_file():
        logger.warning("Documents md publish skipped; missing file: %s", src)
        return None

    name = os.path.basename(file_name or src.name)
    if not name.lower().endswith(".md"):
        name = f"{os.path.splitext(name)[0]}.md"

    local_dest = documents_md_local_artifacts_path(name, user_id=user_id)
    try:
        src_stat = src.stat()
        if (
            os.path.isfile(local_dest)
            and os.path.getsize(local_dest) == src_stat.st_size
            and os.path.getmtime(local_dest) >= src_stat.st_mtime
            and s3_bucket
        ):
            s3_key = documents_md_artifacts_s3_key(name, user_id=user_id)
            runtime_key = documents_md_runtime_workspace_s3_key(name, user_id=user_id)
            public_url = documents_md_artifacts_public_url(name, user_id=user_id)
            head = _documents_head_s3_object_quiet(s3_key)
            runtime_head = _documents_head_s3_object_quiet(runtime_key)
            size_ok = (
                head and int(head.get("content_length") or 0) == src_stat.st_size
            )
            runtime_ok = (
                runtime_head
                and int(runtime_head.get("content_length") or 0) == src_stat.st_size
            )
            if size_ok and runtime_ok:
                return {
                    "file_name": name,
                    "local_path": local_dest,
                    "s3_key": s3_key,
                    "runtime_s3_key": runtime_key,
                    "url": public_url,
                    "uploaded": True,
                    "runtime_mirrored": True,
                    "skipped": True,
                    "bytes": src_stat.st_size,
                }
        if os.path.abspath(str(src)) != os.path.abspath(local_dest):
            import shutil

            shutil.copy2(src, local_dest)
    except Exception:
        logger.exception("Failed to copy documents md to local artifacts: %s", src)
        local_dest = str(src.resolve())

    s3_key = documents_md_artifacts_s3_key(name, user_id=user_id)
    runtime_key = documents_md_runtime_workspace_s3_key(name, user_id=user_id)
    public_url = documents_md_artifacts_public_url(name, user_id=user_id)
    result = {
        "file_name": name,
        "local_path": local_dest,
        "s3_key": s3_key,
        "runtime_s3_key": runtime_key,
        "url": public_url,
        "uploaded": False,
        "runtime_mirrored": False,
    }

    if not s3_bucket:
        logger.warning("s3_bucket not configured; documents md kept local only")
        return result

    try:
        with _without_env_proxies():
            s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
            content_type = get_contents_type(name)
            if content_type == "no info":
                content_type = "text/markdown; charset=utf-8"
            with open(local_dest, "rb") as f:
                body = f.read()
            put_kwargs = {
                "Bucket": s3_bucket,
                "Body": body,
                "ContentType": content_type,
                "CacheControl": "no-cache, max-age=0, must-revalidate",
            }
            s3_client.put_object(Key=s3_key, **put_kwargs)
            result["uploaded"] = True
            result["bytes"] = len(body)
            try:
                s3_client.put_object(Key=runtime_key, **put_kwargs)
                result["runtime_mirrored"] = True
            except Exception:
                logger.exception(
                    "Documents md Runtime workspace mirror failed key=%s", runtime_key
                )
        return result
    except Exception:
        logger.error(
            "Error publishing documents md to artifacts: %s", traceback.format_exc()
        )
        return result


def head_documents_pdf_on_s3(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
) -> bool:
    if kind == "drawing":
        key = documents_drawings_s3_key(file_name, user_id=user_id)
    else:
        key = documents_projects_s3_key(file_name, user_id=user_id)
    if not s3_bucket or not key:
        return False
    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        s3_client.head_object(Bucket=s3_bucket, Key=key)
        return True
    except Exception:
        return False


def documents_pdf_s3_key_for_kind(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
) -> str | None:
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    if kind == "drawing":
        return documents_drawings_s3_key(safe_name, user_id=user_id)
    return documents_projects_s3_key(safe_name, user_id=user_id)


def _documents_content_disposition(file_name: str, *, disposition: str = "attachment") -> str:
    raw = (file_name or "download").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = raw.encode("ascii", "ignore").decode("ascii").strip(" .") or "download"
    ascii_name = re.sub(r"_+", "_", ascii_name).strip("._") or "download"
    _, ext = os.path.splitext(raw)
    if ext and not ascii_name.lower().endswith(ext.lower()):
        base = ascii_name if ascii_name != "download" else "download"
        ascii_name = f"{base}{ext}"
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(raw)}"
    )


def stream_documents_pdf_from_s3(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
):
    from fastapi.responses import StreamingResponse

    key = documents_pdf_s3_key_for_kind(file_name, user_id=user_id, kind=kind)
    if not s3_bucket or not key:
        return None
    safe_name = os.path.basename(file_name or "").strip() or "document.pdf"
    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        obj = s3_client.get_object(Bucket=s3_bucket, Key=key)
        body = obj["Body"]
        content_type = obj.get("ContentType") or "application/pdf"
        if content_type in ("binary/octet-stream", "no info", "application/octet-stream"):
            content_type = "application/pdf"
        return StreamingResponse(
            body.iter_chunks(chunk_size=1024 * 256),
            media_type=content_type,
            headers={
                "Content-Disposition": _documents_content_disposition(
                    safe_name, disposition="inline"
                ),
                "Cache-Control": "private, max-age=3600",
            },
        )
    except Exception:
        logger.error(
            "Error streaming documents pdf from S3 key=%s: %s",
            key,
            traceback.format_exc(),
        )
        return None


def enrich_documents_for_ui(
    documents: list[dict],
    user_id: str | None = None,
    *,
    publish_md: bool = True,
    kind: str = "project",
) -> list[dict]:
    """Attach pdf/md view URLs for Projects / Drawings UI."""
    if kind == "drawing":
        docs_root = documents_drawings_dir(user_id)
    else:
        docs_root = documents_projects_dir(user_id)
        kind = "project"
    kind_qs = f"?kind={kind}"

    enriched: list[dict] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        item = dict(doc)
        filename = str(item.get("filename") or "").strip()
        md_file = str(item.get("md_file") or item.get("md_path") or "").strip()
        md_name = os.path.basename(md_file) if md_file else ""
        if not md_name and filename:
            stem = os.path.splitext(filename)[0]
            md_name = f"{stem}.md"

        pdf_name = filename if filename.lower().endswith(".pdf") else ""
        if not pdf_name and filename:
            src = str(item.get("source_path") or "")
            if src.lower().endswith(".pdf"):
                pdf_name = os.path.basename(src)

        local_md = str(item.get("md_path") or "").strip()
        if local_md and not os.path.isfile(local_md) and md_name:
            candidate = os.path.join(docs_root, md_name)
            if os.path.isfile(candidate):
                local_md = candidate
        elif not local_md and md_name:
            candidate = os.path.join(docs_root, md_name)
            if os.path.isfile(candidate):
                local_md = candidate

        local_pdf = ""
        if pdf_name:
            candidate = os.path.join(docs_root, pdf_name)
            if os.path.isfile(candidate):
                local_pdf = candidate
            else:
                src = str(item.get("source_path") or "")
                if src and os.path.isfile(src) and src.lower().endswith(".pdf"):
                    local_pdf = src

        if kind == "drawing":
            pdf_cf = (
                documents_drawing_pdf_public_url(pdf_name, user_id=user_id)
                if pdf_name
                else None
            )
        else:
            pdf_cf = (
                documents_project_pdf_public_url(pdf_name, user_id=user_id)
                if pdf_name
                else None
            )
        pdf_on_s3 = bool(
            pdf_name
            and head_documents_pdf_on_s3(pdf_name, user_id=user_id, kind=kind)
        )
        item["pdf_available"] = bool(local_pdf) or pdf_on_s3
        item["pdf_url"] = pdf_cf if pdf_on_s3 else None
        item["pdf_api_url"] = (
            f"/api/documents/documents/{parse.quote(pdf_name)}/pdf{kind_qs}"
            if pdf_name
            else None
        )

        md_url = None
        md_published = False
        if local_md and os.path.isfile(local_md) and publish_md:
            published = publish_documents_markdown_to_artifacts(
                local_md, user_id=user_id, file_name=md_name or None
            )
            if published:
                md_url = published.get("url")
                md_published = bool(published.get("uploaded"))
                item["md_s3_key"] = published.get("s3_key")
                item["md_local_artifacts"] = published.get("local_path")
        elif md_name:
            md_url = documents_md_artifacts_public_url(md_name, user_id=user_id)

        item["md_available"] = bool(local_md and os.path.isfile(local_md))
        item["md_url"] = md_url
        item["md_published"] = md_published
        if local_md and os.path.isfile(local_md):
            try:
                item["md_bytes"] = os.path.getsize(local_md)
            except OSError:
                item["md_bytes"] = None
        else:
            item["md_bytes"] = None
        item["md_viewer_url"] = (
            f"/api/documents/documents/{parse.quote(md_name)}/markdown{kind_qs}"
            if md_name
            else None
        )
        segment = sanitize_user_path_segment(user_id) or "default"
        if md_name:
            item["md_workspace_path"] = (
                f"/mnt/workspace/{segment}/artifacts/md/{md_name}"
            )
        item["display_name"] = (
            str(item.get("original_filename") or "").strip() or filename or md_name
        )
        item["kind"] = kind
        enriched.append(item)
    return enriched


def _documents_unlink_under_roots(path: str, *roots: str) -> bool:
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    allowed = False
    for root in roots:
        try:
            root_real = os.path.realpath(root)
        except OSError:
            continue
        if resolved == root_real or resolved.startswith(root_real + os.sep):
            allowed = True
            break
    if not allowed:
        return False
    try:
        if os.path.isfile(resolved):
            os.unlink(resolved)
            return True
    except OSError:
        return False
    return False


def _documents_delete_s3_key_quiet(s3_key: str | None) -> bool:
    if not s3_bucket or not s3_key:
        return False
    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        s3_client.delete_object(Bucket=s3_bucket, Key=s3_key)
        return True
    except Exception:
        return False


def delete_documents_document(
    user_id: str | None,
    filename: str,
    *,
    kind: str = "project",
) -> dict:
    """Remove one Documents entry: local source + sidecars + list entry (+ S3)."""
    import shutil
    from pathlib import Path as _Path

    name = os.path.basename(filename or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError("Invalid document name")

    kind_norm = (kind or "project").strip().lower()
    if kind_norm not in {"project", "drawing"}:
        raise ValueError(f"Unsupported kind: {kind}")

    _ensure_documents_on_path()
    from doc_list import DRAWINGS, PROJECTS, get_document, remove_document

    registry = {"project": PROJECTS, "drawing": DRAWINGS}[kind_norm]
    root = ensure_user_documents_dir(user_id)
    artifacts_root = ensure_user_artifacts_dir(user_id)
    docs_dir = {
        "project": documents_projects_dir(user_id),
        "drawing": documents_drawings_dir(user_id),
    }[kind_norm]

    entry = get_document(root, filename=name, registry=registry)
    if entry is None:
        stem = os.path.splitext(name)[0]
        entry = {
            "filename": name,
            "source_path": os.path.join(docs_dir, name),
            "md_path": os.path.join(docs_dir, f"{stem}.md"),
            "json_path": os.path.join(docs_dir, f"{stem}.json"),
        }
        exists = any(
            p and os.path.isfile(p)
            for p in (
                entry["source_path"],
                entry.get("md_path"),
                entry.get("json_path"),
            )
        )
        if not exists:
            raise FileNotFoundError(f"Document not found: {name}")

    stem = os.path.splitext(str(entry.get("filename") or name))[0] or os.path.splitext(
        name
    )[0]
    deleted_files: list[str] = []
    allow_roots = (root, artifacts_root, docs_dir)

    paths_to_delete: list[str] = []
    for key in ("source_path", "md_path", "json_path"):
        raw = str(entry.get(key) or "").strip()
        if raw:
            paths_to_delete.append(raw)

    for sibling in (f"{stem}.pdf", f"{stem}.md", f"{stem}.json", name):
        paths_to_delete.append(os.path.join(docs_dir, sibling))
    md_file = str(entry.get("md_file") or "").strip()
    if md_file:
        paths_to_delete.append(os.path.join(docs_dir, os.path.basename(md_file)))
        paths_to_delete.append(documents_md_local_artifacts_path(md_file, user_id=user_id))
    else:
        paths_to_delete.append(
            documents_md_local_artifacts_path(f"{stem}.md", user_id=user_id)
        )

    seen: set[str] = set()
    for path in paths_to_delete:
        try:
            resolved = str(_Path(path).expanduser().resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _documents_unlink_under_roots(resolved, *allow_roots):
            deleted_files.append(resolved)

    pages_root = _Path(documents_converted_dir(user_id)) / ".pdf_pages"
    deleted_dirs: list[str] = []
    if pages_root.is_dir() and stem:
        for work in pages_root.iterdir():
            if not work.is_dir():
                continue
            if work.name == stem or work.name.startswith(f"{stem}_"):
                try:
                    shutil.rmtree(work)
                    deleted_dirs.append(str(work))
                except OSError:
                    logger.warning("Failed to remove pdf_pages dir: %s", work)

    s3_deleted: list[str] = []
    entry_s3 = str(entry.get("s3_key") or "").strip()
    if entry_s3 and _documents_delete_s3_key_quiet(entry_s3):
        s3_deleted.append(entry_s3)

    if kind_norm == "drawing":
        pdf_key = documents_drawings_s3_key(f"{stem}.pdf", user_id=user_id)
    else:
        pdf_key = documents_projects_s3_key(f"{stem}.pdf", user_id=user_id)
    if pdf_key and pdf_key not in s3_deleted and _documents_delete_s3_key_quiet(pdf_key):
        s3_deleted.append(pdf_key)

    md_key = documents_md_artifacts_s3_key(f"{stem}.md", user_id=user_id)
    if md_key not in s3_deleted and _documents_delete_s3_key_quiet(md_key):
        s3_deleted.append(md_key)

    removed = remove_document(root, filename=name, registry=registry)
    if not removed and entry.get("source_path"):
        removed = remove_document(
            root, source_path=str(entry.get("source_path")), registry=registry
        )

    if not removed and not deleted_files and not deleted_dirs:
        raise FileNotFoundError(f"Document not found: {name}")

    return {
        "ok": True,
        "filename": name,
        "kind": kind_norm,
        "removed_from_list": bool(removed),
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
        "s3_deleted": s3_deleted,
    }
