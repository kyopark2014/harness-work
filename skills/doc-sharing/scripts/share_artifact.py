#!/usr/bin/env python3
"""Upload a local artifact to the project S3 sharing prefix and print a CloudFront URL.

Copies (PutObject) from the Code Interpreter local filesystem into:
  s3://{S3_BUCKET}/artifacts/{actor_id}/...

Then returns:
  {SHARING_URL}/artifacts/{actor_id}/...
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from typing import Optional, Tuple
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

_SESSION_MOUNT = "/mnt/workspace"
_ALLOWED_PREFIXES = ("artifacts", "images", "docs")


def _load_sidecar_config() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "..", "config.json"),
        os.path.join(here, "config.json"),
    ):
        try:
            with open(os.path.normpath(path), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"warning: failed to load {path}: {e}", file=sys.stderr)
    return {}


def _sanitize_actor(actor_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]", "_", (actor_id or "").strip()) or "user"


def _content_type(key: str) -> str:
    guessed, _ = mimetypes.guess_type(key)
    return guessed or "application/octet-stream"


def _basename(filepath: str) -> str:
    name = os.path.basename(filepath.replace("\\", "/").rstrip("/")) or "upload.bin"
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name.replace("..", "_")) or "upload.bin"


def _infer_actor_and_rest(filepath: str, actor_id: Optional[str]) -> Tuple[str, str, str]:
    """Return (actor, dest_prefix, rest_under_prefix)."""
    path = os.path.abspath(os.path.expanduser(filepath)).replace("\\", "/")
    mount = (
        os.environ.get("SESSION_STORAGE_DIR")
        or os.environ.get("S3_FILES_MOUNT_PATH")
        or _SESSION_MOUNT
    ).rstrip("/")

    rel = path
    if path.startswith(f"{mount}/"):
        rel = path[len(mount) + 1 :]
    elif path.startswith(f"{_SESSION_MOUNT}/"):
        rel = path[len(_SESSION_MOUNT) + 1 :]

    parts = [p for p in rel.split("/") if p]
    user = _sanitize_actor(actor_id or "")

    # /mnt/workspace/{user}/artifacts|images|docs/...
    if len(parts) >= 2 and parts[1] in _ALLOWED_PREFIXES:
        inferred_user = _sanitize_actor(parts[0])
        prefix = parts[1]
        rest = "/".join(parts[2:]) or _basename(path)
        return user or inferred_user, prefix, rest

    # artifacts|images|docs/... (relative to cwd or already stripped)
    for prefix in _ALLOWED_PREFIXES:
        if parts and parts[0] == prefix:
            rest_parts = parts[1:]
            if user and rest_parts and rest_parts[0] == user:
                rest_parts = rest_parts[1:]
            rest = "/".join(rest_parts) or _basename(path)
            return user or "user", prefix, rest

    # Fallback: treat as artifacts/{actor}/{filename}
    return user or "user", "artifacts", _basename(path)


def share_artifact(
    filepath: str,
    actor_id: Optional[str] = None,
    bucket: Optional[str] = None,
    sharing_url: Optional[str] = None,
    region: Optional[str] = None,
) -> dict:
    path = os.path.abspath(os.path.expanduser(filepath))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Local file not found: {path}")

    cfg = _load_sidecar_config()
    bucket = (
        bucket
        or os.environ.get("S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
        or cfg.get("s3_bucket")
        or ""
    ).strip()
    if not bucket:
        raise RuntimeError(
            "S3_BUCKET is not set (harness env or skills/doc-sharing/config.json)"
        )

    sharing_url = (
        sharing_url
        or os.environ.get("SHARING_URL")
        or cfg.get("sharing_url")
        or ""
    ).rstrip("/")
    app_url = (
        os.environ.get("APP_URL")
        or os.environ.get("app_url")
        or cfg.get("app_url")
        or ""
    ).rstrip("/")
    region = (
        region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or cfg.get("region")
        or "us-west-2"
    )

    actor, prefix, rest = _infer_actor_and_rest(path, actor_id)
    dest_key = f"{prefix}/{actor}/{rest}".replace("//", "/")
    content_type = _content_type(dest_key)

    s3 = boto3.client("s3", region_name=region)
    extra = {"ContentType": content_type}
    try:
        s3.upload_file(path, bucket, dest_key, ExtraArgs=extra)
    except ClientError as e:
        raise RuntimeError(f"S3 upload failed s3://{bucket}/{dest_key}: {e}") from e

    if sharing_url:
        url = f"{sharing_url}/{quote(dest_key, safe='/')}"
    else:
        url = f"https://{bucket}.s3.{region}.amazonaws.com/{quote(dest_key, safe='/')}"

    viewer_url = None
    lower_rest = rest.lower()
    if app_url and prefix == "artifacts" and (
        lower_rest.endswith(".md")
        or lower_rest.endswith(".markdown")
        or lower_rest.endswith(".json")
        or lower_rest.endswith(".csv")
    ):
        viewer_url = f"{app_url}/api/artifacts/view/{quote(rest, safe='/')}"

    return {
        "ok": True,
        "bucket": bucket,
        "key": dest_key,
        "url": url,
        "viewer_url": viewer_url,
        "actor_id": actor,
        "local_path": path,
        "content_type": content_type,
        "size": os.path.getsize(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Share a local artifact via S3 + CloudFront URL"
    )
    parser.add_argument(
        "--filepath",
        "-f",
        required=True,
        help="Local absolute path under ARTIFACTS_DIR (or images/docs)",
    )
    parser.add_argument(
        "--actor-id",
        "-a",
        default=os.environ.get("ACTOR_ID") or os.environ.get("actor_id") or "",
        help="Actor / user id (defaults to path segment under /mnt/workspace)",
    )
    parser.add_argument("--bucket", default="", help="Override S3_BUCKET")
    parser.add_argument("--sharing-url", default="", help="Override SHARING_URL")
    parser.add_argument("--region", default="", help="Override AWS_REGION")
    args = parser.parse_args()

    try:
        result = share_artifact(
            filepath=args.filepath,
            actor_id=args.actor_id or None,
            bucket=args.bucket or None,
            sharing_url=args.sharing_url or None,
            region=args.region or None,
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
