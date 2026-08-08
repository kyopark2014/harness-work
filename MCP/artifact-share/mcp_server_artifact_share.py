"""Streamable-HTTP MCP server: copy session artifacts to CloudFront sharing keys.

Harness writes under ARTIFACTS_DIR ``/mnt/workspace/{actor_id}/artifacts``.
S3 Files stores that as ``agentcore-sessions/{actor_id}/artifacts/...``.
This server copies to ``artifacts/{actor_id}/...`` (``s3:CopyObject``) and
returns the CloudFront download URL.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import time
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("artifact-share")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")

_SESSION_MOUNT = "/mnt/workspace"
_S3_FILES_SESSION_PREFIX = "agentcore-sessions/"
_ALLOWED_DEST_PREFIXES = ("artifacts/", "images/", "docs/")
# S3 Files can lag ~60s behind /mnt/workspace writes; retry HeadObject.
_SYNC_MAX_ATTEMPTS = max(1, int(os.environ.get("S3_FILES_SYNC_MAX_ATTEMPTS", "10")))
_SYNC_BASE_DELAY_SEC = max(0.5, float(os.environ.get("S3_FILES_SYNC_BASE_DELAY_SEC", "3")))
_SYNC_MAX_DELAY_SEC = max(
    _SYNC_BASE_DELAY_SEC, float(os.environ.get("S3_FILES_SYNC_MAX_DELAY_SEC", "20"))
)


def _load_config() -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to load config.json: %s", e)
        return {}


_config = _load_config()
_region = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or _config.get("region")
    or "us-west-2"
)
_bucket = (
    os.environ.get("S3_BUCKET")
    or os.environ.get("AWS_S3_BUCKET")
    or _config.get("s3_bucket")
    or ""
)
_sharing_url = (
    os.environ.get("SHARING_URL") or _config.get("sharing_url") or ""
).rstrip("/")
_session_mount = (
    os.environ.get("SESSION_STORAGE_DIR")
    or os.environ.get("S3_FILES_MOUNT_PATH")
    or _SESSION_MOUNT
).rstrip("/")
_session_prefix = (
    os.environ.get("S3_FILES_SESSION_PREFIX")
    or _config.get("s3_files_session_prefix")
    or _S3_FILES_SESSION_PREFIX
).strip("/")
if _session_prefix:
    _session_prefix = f"{_session_prefix}/"

logger.info(
    "artifact-share config: bucket=%s region=%s sharing_url=%s session_prefix=%s",
    _bucket or "(missing)",
    _region,
    _sharing_url or "(none)",
    _session_prefix or "(none)",
)


def _sanitize_actor(actor_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]", "_", actor_id.strip()) or ""


def _content_type(key: str) -> str:
    guessed, _ = mimetypes.guess_type(key)
    return guessed or "application/octet-stream"


def _basename(filepath: str) -> str:
    name = os.path.basename(filepath.replace("\\", "/").rstrip("/")) or "upload.bin"
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name.replace("..", "_")) or "upload.bin"


def _mount_relative(filepath: str) -> str:
    """Strip /mnt/workspace (or SESSION_STORAGE_DIR) → mount-relative path."""
    path = filepath.replace("\\", "/").lstrip("./")
    for prefix in (f"{_session_mount}/", f"{_SESSION_MOUNT}/", _SESSION_MOUNT):
        if path.startswith(prefix):
            return path[len(prefix) :].lstrip("/")
        if path == prefix.rstrip("/"):
            return ""
    if path.startswith("mnt/workspace/"):
        return path[len("mnt/workspace/") :]
    return path.lstrip("/")


def _artifact_relpath(filepath: str, actor_id: str) -> tuple[str, str]:
    """Return ``(prefix, rest)`` where dest key is ``{prefix}/{actor_id}/{rest}``."""
    user = _sanitize_actor(actor_id)
    mount_rel = _mount_relative(filepath)
    parts = [p for p in mount_rel.split("/") if p]

    if parts and parts[0] == _session_prefix.rstrip("/"):
        parts = parts[1:]
        mount_rel = "/".join(parts)

    # /mnt/workspace/{user}/artifacts|images|docs/...
    if (
        user
        and len(parts) >= 2
        and parts[0] == user
        and parts[1] in ("artifacts", "images", "docs")
    ):
        return parts[1], "/".join(parts[2:]) or _basename(filepath)

    # artifacts|images|docs/... (optionally already including {user}/)
    for prefix in ("artifacts", "images", "docs"):
        head = f"{prefix}/"
        if mount_rel == prefix or mount_rel.startswith(head):
            rest = "" if mount_rel == prefix else mount_rel[len(head) :]
            if user and (rest == user or rest.startswith(f"{user}/")):
                rest = rest[len(user) :].lstrip("/")
            return prefix, rest or _basename(filepath)

    name = _basename(filepath)
    if _content_type(name).startswith("image/"):
        return "images", name
    return "artifacts", name


def _dest_key(filepath: str, actor_id: str) -> str:
    user = _sanitize_actor(actor_id)
    prefix, rest = _artifact_relpath(filepath, actor_id)
    rest = (rest or "").lstrip("/")
    return f"{prefix}/{user}/{rest}" if rest else f"{prefix}/{user}/"


def _source_keys(filepath: str, actor_id: str, dest_key: str) -> list[str]:
    """Prefer S3 Files session key; a few legacy layouts as fallback."""
    user = _sanitize_actor(actor_id)
    prefix, rest = _artifact_relpath(filepath, actor_id)
    rest = (rest or "").lstrip("/")
    name = _basename(filepath)
    mount_rel = _mount_relative(filepath)

    keys: list[str] = []

    def _add(key: str) -> None:
        key = key.lstrip("/")
        if key and key not in keys:
            keys.append(key)

    if user and _session_prefix and rest:
        _add(f"{_session_prefix}{user}/{prefix}/{rest}")
    if user and _session_prefix:
        _add(f"{_session_prefix}{user}/{prefix}/{name}")
        _add(f"{_session_prefix}{mount_rel}")
    if user and rest:
        _add(f"{user}/{prefix}/{rest}")
        _add(f"{prefix}/{user}/{rest}")
    _add(dest_key)
    return keys


def _public_url(key: str) -> str:
    if _sharing_url:
        quoted = "/".join(quote(seg) for seg in key.split("/") if seg)
        return f"{_sharing_url}/{quoted}"
    return (
        f"https://{_region}.console.aws.amazon.com/s3/object/{_bucket}"
        f"?prefix={quote(key, safe='')}"
    )


def _head_first_existing(client, candidates: list[str]) -> str | None:
    for source in candidates:
        try:
            client.head_object(Bucket=_bucket, Key=source)
            return source
        except ClientError:
            continue
    return None


def _sync_retry_delays() -> list[float]:
    """Immediate first try, then capped exponential backoff (covers S3 Files lag)."""
    delays = [0.0]
    delay = _SYNC_BASE_DELAY_SEC
    for _ in range(_SYNC_MAX_ATTEMPTS - 1):
        delays.append(delay)
        delay = min(delay * 2, _SYNC_MAX_DELAY_SEC)
    return delays


def _copy_to_sharing(filepath: str, actor_id: str) -> str:
    uid = (actor_id or "").strip()
    if not uid:
        return "Share failed: actor_id is required"
    if not _bucket:
        return "Share failed: S3 bucket is not configured (set S3_BUCKET)"
    if not filepath or not str(filepath).strip():
        return "Share failed: filepath is required"

    filepath = str(filepath).strip()
    dest = _dest_key(filepath, uid)
    if not dest.startswith(_ALLOWED_DEST_PREFIXES):
        return (
            "Share failed: S3 key must start with "
            f"{', '.join(_ALLOWED_DEST_PREFIXES)} (got {dest!r})"
        )

    client = boto3.client("s3", region_name=_region)
    candidates = _source_keys(filepath, uid, dest)

    try:
        source = None
        delays = _sync_retry_delays()
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                logger.info(
                    "source not visible yet (S3 Files sync); "
                    "retry %s/%s after %.1fs for %r",
                    attempt,
                    len(delays),
                    delay,
                    filepath,
                )
                time.sleep(delay)
            source = _head_first_existing(client, candidates)
            if source:
                if attempt > 1:
                    logger.info(
                        "found source after %s attempt(s): s3://%s/%s",
                        attempt,
                        _bucket,
                        source,
                    )
                break

        if not source:
            tried = ", ".join(candidates[:4])
            waited = sum(delays)
            return (
                f"Share failed: File not found for {filepath!r} "
                f"(looked under s3://{_bucket}/… e.g. {tried}; "
                f"retried {len(delays)}x over ~{waited:.0f}s). "
                "Ensure the file exists under ARTIFACTS_DIR "
                f"(/mnt/workspace/{uid}/artifacts) so it syncs to "
                f"{_session_prefix}{uid}/artifacts/..."
            )

        if source != dest:
            content_type = _content_type(dest)
            extra = {
                "ContentType": content_type,
                "MetadataDirective": "REPLACE",
            }
            if content_type == "application/pdf":
                extra["ContentDisposition"] = "inline"
            client.copy_object(
                Bucket=_bucket,
                Key=dest,
                CopySource={"Bucket": _bucket, "Key": source},
                **extra,
            )
            logger.info(
                "copied s3://%s/%s → s3://%s/%s",
                _bucket,
                source,
                _bucket,
                dest,
            )
        else:
            logger.info("object already at sharing key: %s", dest)

        return f"Share complete: {_public_url(dest)}"
    except Exception as e:
        logger.error("share failed: %s", e)
        return f"Share failed: {e}"


try:
    mcp = FastMCP(
        name="artifact-share",
        instructions=(
            "You copy Harness ARTIFACTS_DIR files to the CloudFront sharing "
            "prefix on the project S3 bucket and return download URLs. "
            "Always pass actor_id from the system prompt (account login id) "
            "— never a nickname. Pass filepath as "
            "'/mnt/workspace/{actor_id}/artifacts/<file>' or 'artifacts/<file>'. "
            "The server copies "
            "agentcore-sessions/{actor_id}/artifacts/... → "
            "artifacts/{actor_id}/... (S3 CopyObject)."
        ),
        host="0.0.0.0",
        stateless_http=True,
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
    logger.info("Error: %s", e)
    raise


@mcp.tool()
def share_artifact(filepath: str, actor_id: str) -> str:
    """
    Copy a session artifact to the sharing S3 prefix and return a CloudFront URL.

    Copies like ``aws s3 cp`` within the project bucket:
    agentcore-sessions/{actor_id}/artifacts/... → artifacts/{actor_id}/...

    filepath: ARTIFACTS_DIR path, e.g.
        '/mnt/workspace/{actor_id}/artifacts/report.pdf' or 'artifacts/report.pdf'.
    actor_id: account login id from the system prompt. Do NOT use a nickname.
    return: 'Share complete: <url>' or an error message.
    """
    logger.info("share_artifact --> filepath=%s actor_id=%s", filepath, actor_id)
    try:
        result = _copy_to_sharing(filepath, actor_id)
        logger.info("result: %s", result[:200] if result else result)
        return result
    except Exception as e:
        logger.error("Error in share_artifact: %s", e)
        return f"Share failed: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
