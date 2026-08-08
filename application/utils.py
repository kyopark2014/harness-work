import logging
import sys
import json
import traceback
import boto3
import os
from urllib import parse

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

    Always includes base MCP servers (knowledge base, artifact-share).
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


_DEFAULT_USER_SETTINGS: dict[str, bool] = {
    "knowledge_graph_enabled": True,
}


def get_user_settings_path(user_id: str | None) -> str:
    """Absolute path to {SESSION_STORAGE_DIR}/{user_id}/settings.json (does not create)."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "settings.json")


def load_user_settings(user_id: str | None) -> dict[str, bool]:
    """Load per-user UI/feature settings. Missing file → defaults (KG on)."""
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
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load user settings %s: %s", path, e)
    return settings


def save_user_settings(user_id: str | None, **updates: bool) -> dict[str, bool]:
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
        if key in _DEFAULT_USER_SETTINGS:
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

