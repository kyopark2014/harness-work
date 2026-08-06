#!/usr/bin/env python3
"""Upload a local file to the project S3 bucket and print a download URL.

Mirrors langgraph ``upload_file_to_s3``: put_object under
``artifacts|images|docs/{user_id}/...``, then return CloudFront sharing URL
(or S3 console URL when SHARING_URL is unset).
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
from urllib.parse import quote

_ALLOWED_S3_PREFIXES = ("artifacts/", "images/", "docs/")
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._\-/= ]+")

_EXT_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".py": "text/x-python",
    ".json": "application/json",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".xls": "application/vnd.ms-excel",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ),
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def sanitize_user_segment(user_id: str | None) -> str | None:
    if not user_id:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._\-]", "_", user_id.strip())
    return cleaned or None


def get_contents_type(file_name: str) -> str:
    ext = os.path.splitext(file_name)[1].lower()
    if ext in _EXT_CONTENT_TYPES:
        return _EXT_CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def s3_uri_to_console_url(uri: str, region: str) -> str:
    if not uri or not uri.startswith("s3://"):
        return ""
    rest = uri[5:]
    parts = rest.split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    return (
        f"https://{region}.console.aws.amazon.com/s3/object/{bucket}"
        f"?prefix={quote(key, safe='')}"
    )


def public_url_for_key(sharing_url: str, key: str) -> str:
    base = sharing_url.rstrip("/")
    quoted = "/".join(quote(seg) for seg in key.split("/") if seg != "")
    return f"{base}/{quoted}"


def _safe_basename(filepath: str) -> str:
    name = os.path.basename(filepath.replace("\\", "/").rstrip("/")) or "upload.bin"
    name = name.replace("..", "_").strip("._") or "upload.bin"
    return _UNSAFE_KEY_CHARS.sub("_", name)


def _strip_to_allowed_prefix(normalized: str) -> str:
    for prefix in _ALLOWED_S3_PREFIXES:
        idx = normalized.find(prefix)
        if idx != -1:
            return normalized[idx:]
    if normalized == "artifacts":
        return "artifacts/"
    return normalized


def resolve_local_path(filepath: str, artifacts_dir: str, session_dir: str) -> str:
    """Resolve filepath relative to artifacts / session storage / cwd."""
    if not filepath:
        raise ValueError("filepath is required")

    filepath = os.path.expanduser(filepath)
    if os.path.isabs(filepath):
        if os.path.exists(filepath):
            return filepath
        basename = os.path.basename(filepath.rstrip("/"))
        if basename and artifacts_dir:
            candidate = os.path.join(artifacts_dir, basename)
            if os.path.exists(candidate):
                return candidate
        return filepath

    normalized = _strip_to_allowed_prefix(
        filepath.replace("\\", "/").lstrip("./")
    )

    if normalized == "artifacts" or normalized.startswith("artifacts/"):
        suffix = normalized[len("artifacts") :].lstrip("/")
        user = sanitize_user_segment(
            _env("AGENTCORE_USER_ID", "ACTOR_ID", "USER_ID")
        )
        if user and (suffix == user or suffix.startswith(f"{user}/")):
            suffix = suffix[len(user) :].lstrip("/")
        if artifacts_dir:
            candidate = (
                os.path.join(artifacts_dir, suffix) if suffix else artifacts_dir
            )
            if os.path.exists(candidate):
                return candidate

    for root in (os.getcwd(), session_dir, artifacts_dir):
        if not root:
            continue
        candidate = os.path.join(root, filepath)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(root, normalized)
        if os.path.exists(candidate):
            return candidate

    basename = os.path.basename(normalized.rstrip("/"))
    if basename and artifacts_dir:
        candidate = os.path.join(artifacts_dir, basename)
        if os.path.exists(candidate):
            return candidate

    return os.path.abspath(filepath)


def s3_key_with_user(prefix: str, rest: str, user_id: str | None) -> str:
    rest = (rest or "").lstrip("/")
    user = sanitize_user_segment(user_id)
    if user:
        if rest == user or rest.startswith(f"{user}/"):
            return f"{prefix}/{rest}" if rest else f"{prefix}/{user}/"
        return f"{prefix}/{user}/{rest}" if rest else f"{prefix}/{user}/"
    return f"{prefix}/{rest}" if rest else f"{prefix}/"


def build_s3_key(
    filepath: str,
    full_path: str,
    *,
    artifacts_dir: str,
    session_dir: str,
    user_id: str | None,
) -> str:
    """Map a local file onto ``artifacts|images|docs/{user_id}/...``."""
    normalized = _strip_to_allowed_prefix(
        filepath.replace("\\", "/").lstrip("./")
    )

    for prefix in ("artifacts", "images", "docs"):
        head = f"{prefix}/"
        if normalized.startswith(head) or normalized == prefix:
            rest = "" if normalized == prefix else normalized[len(head) :]
            return s3_key_with_user(prefix, rest, user_id)

    try:
        if artifacts_dir and os.path.isdir(artifacts_dir):
            artifacts_real = os.path.realpath(artifacts_dir)
            full_real = os.path.realpath(full_path)
            if os.path.commonpath([full_real, artifacts_real]) == artifacts_real:
                rel = os.path.relpath(full_real, artifacts_real).replace("\\", "/")
                return s3_key_with_user(
                    "artifacts", "" if rel == "." else rel, user_id
                )
    except (OSError, ValueError):
        pass

    try:
        if session_dir and os.path.isdir(session_dir):
            session_real = os.path.realpath(session_dir)
            full_real = os.path.realpath(full_path)
            if os.path.commonpath([full_real, session_real]) == session_real:
                rel = os.path.relpath(full_real, session_real).replace("\\", "/")
                parts = rel.split("/")
                if len(parts) >= 2 and parts[1] == "artifacts":
                    user_seg = parts[0]
                    rest = "/".join(parts[2:])
                    if user_seg:
                        return (
                            f"artifacts/{user_seg}/{rest}"
                            if rest
                            else f"artifacts/{user_seg}/"
                        )
                if parts and parts[0] == "artifacts":
                    rest = "/".join(parts[1:])
                    return s3_key_with_user("artifacts", rest, user_id)
    except (OSError, ValueError):
        pass

    basename = _safe_basename(filepath)
    content_type = get_contents_type(basename)
    if content_type.startswith("image/"):
        return s3_key_with_user("images", basename, user_id)
    return s3_key_with_user("artifacts", basename, user_id)


def resolve_artifacts_dir(session_dir: str, user_id: str | None) -> str:
    explicit = _env("ARTIFACTS_DIR")
    if explicit:
        return explicit
    user = sanitize_user_segment(user_id)
    if session_dir and user:
        candidate = os.path.join(session_dir, user, "artifacts")
        if os.path.isdir(candidate):
            return candidate
    if session_dir:
        candidate = os.path.join(session_dir, "artifacts")
        if os.path.isdir(candidate):
            return candidate
    cwd_artifacts = os.path.join(os.getcwd(), "artifacts")
    if os.path.isdir(cwd_artifacts):
        return cwd_artifacts
    return session_dir or os.getcwd()


def upload_file_to_s3(
    filepath: str,
    *,
    bucket: str,
    region: str,
    sharing_url: str | None,
    user_id: str | None,
    session_dir: str,
) -> str:
    import boto3

    if not bucket:
        raise RuntimeError(
            "S3 bucket is not configured. Set S3_BUCKET or pass --bucket."
        )

    artifacts_dir = resolve_artifacts_dir(session_dir, user_id)
    resolved = resolve_local_path(filepath, artifacts_dir, session_dir)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(
            f"File not found: {filepath} (resolved: {resolved})"
        )

    key = build_s3_key(
        filepath,
        resolved,
        artifacts_dir=artifacts_dir,
        session_dir=session_dir,
        user_id=user_id,
    )
    if not key.startswith(_ALLOWED_S3_PREFIXES):
        raise ValueError(
            "Upload rejected: S3 key must start with "
            f"{', '.join(_ALLOWED_S3_PREFIXES)} (got {key!r})"
        )

    content_type = get_contents_type(key)
    s3 = boto3.client("s3", region_name=region)
    with open(resolved, "rb") as f:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=f.read(),
            ContentType=content_type,
        )

    if sharing_url:
        return f"Upload complete: {public_url_for_key(sharing_url, key)}"
    return (
        "Upload complete: "
        f"{s3_uri_to_console_url(f's3://{bucket}/{key}', region)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a local file to project S3 and print the download URL "
            "(CloudFront sharing URL when SHARING_URL is set)."
        )
    )
    parser.add_argument(
        "filepath",
        help=(
            "Local path. Prefer artifacts/... or an absolute path under "
            "SESSION_STORAGE_DIR / ARTIFACTS_DIR."
        ),
    )
    parser.add_argument(
        "--bucket",
        default=_env("S3_BUCKET", "AWS_S3_BUCKET"),
        help="Project S3 bucket (default: S3_BUCKET env)",
    )
    parser.add_argument(
        "--sharing-url",
        default=_env("SHARING_URL", "SHARING_BASE_URL"),
        help="CloudFront base URL (default: SHARING_URL env)",
    )
    parser.add_argument(
        "--region",
        default=_env("AWS_REGION", "AWS_DEFAULT_REGION", default="us-west-2"),
        help="AWS region (default: AWS_REGION or us-west-2)",
    )
    parser.add_argument(
        "--user-id",
        default=_env("AGENTCORE_USER_ID", "ACTOR_ID", "USER_ID"),
        help="User segment for S3 key (default: AGENTCORE_USER_ID / ACTOR_ID)",
    )
    parser.add_argument(
        "--session-dir",
        default=_env(
            "SESSION_STORAGE_DIR",
            "S3_FILES_MOUNT_PATH",
            default="/mnt/workspace",
        ),
        help="Session storage root (default: SESSION_STORAGE_DIR or /mnt/workspace)",
    )
    args = parser.parse_args(argv)

    try:
        message = upload_file_to_s3(
            args.filepath,
            bucket=args.bucket,
            region=args.region,
            sharing_url=args.sharing_url or None,
            user_id=args.user_id or None,
            session_dir=args.session_dir,
        )
        print(message)
        return 0
    except Exception as exc:
        print(f"Upload failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
