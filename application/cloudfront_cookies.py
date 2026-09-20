"""CloudFront signed cookies for /artifacts, /docs, /images.

S3 cache behaviors require a Trusted Key Group. After login the Web UI sets
CloudFront-Policy / CloudFront-Signature / CloudFront-Key-Pair-Id on the
viewer host (same CloudFront domain as sharing_url) so browser clicks on
sharing_url links succeed without making those prefixes world-readable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import timedelta
from typing import Optional

logger = logging.getLogger("cloudfront_cookies")

COOKIE_POLICY = "CloudFront-Policy"
COOKIE_SIGNATURE = "CloudFront-Signature"
COOKIE_KEY_PAIR_ID = "CloudFront-Key-Pair-Id"
ALL_COOKIE_NAMES = (COOKIE_POLICY, COOKIE_SIGNATURE, COOKIE_KEY_PAIR_ID)

DEFAULT_COOKIE_MAX_AGE_SECONDS = int(timedelta(days=30).total_seconds())

_ENV_PRIVATE_KEY = "CLOUDFRONT_SIGNING_PRIVATE_KEY"
_ENV_KEY_PAIR_ID = "CLOUDFRONT_KEY_PAIR_ID"

_cached_private_key = None
_cached_key_pair_id: Optional[str] = None
_load_attempted = False


def _project_name() -> str:
    try:
        try:
            from application import utils
        except ImportError:
            import utils

        name = (utils.load_config().get("projectName") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return (
        os.environ.get("TASK_DB_PROJECT") or "harness-work"
    ).strip() or "harness-work"


def _secret_name() -> str:
    return f"{_project_name()}/cloudfront-signing-key"


def _sharing_url() -> str:
    try:
        try:
            from application import utils
        except ImportError:
            import utils

        return (utils.load_config().get("sharing_url") or "").strip().rstrip("/")
    except Exception:
        return (os.environ.get("SHARING_URL") or "").strip().rstrip("/")


def _load_from_secrets_manager() -> tuple[Optional[str], Optional[str]]:
    try:
        import boto3

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=_secret_name())
        raw = (response.get("SecretString") or "").strip()
        if not raw:
            return None, None
        if raw.startswith("{"):
            data = json.loads(raw)
            return (
                (data.get("private_key_pem") or "").strip() or None,
                (data.get("public_key_id") or "").strip() or None,
            )
        return raw, (os.environ.get(_ENV_KEY_PAIR_ID) or "").strip() or None
    except Exception as e:
        logger.debug(
            "CloudFront signing material not loaded from Secrets Manager: %s",
            type(e).__name__,
        )
        return None, None


def _ensure_material() -> tuple[Optional[object], Optional[str]]:
    global _cached_private_key, _cached_key_pair_id, _load_attempted
    if _load_attempted:
        return _cached_private_key, _cached_key_pair_id
    _load_attempted = True

    private_pem = (os.environ.get(_ENV_PRIVATE_KEY) or "").strip()
    key_pair_id = (os.environ.get(_ENV_KEY_PAIR_ID) or "").strip()
    if not private_pem or not key_pair_id:
        sm_pem, sm_id = _load_from_secrets_manager()
        private_pem = private_pem or (sm_pem or "")
        key_pair_id = key_pair_id or (sm_id or "")

    if not private_pem or not key_pair_id:
        logger.info("CloudFront signed cookies disabled (signing material missing)")
        return None, None

    try:
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(
            private_pem.encode("utf-8"),
            password=None,
        )
    except Exception:
        logger.exception("Failed to load CloudFront signing private key")
        return None, None

    _cached_private_key = private_key
    _cached_key_pair_id = key_pair_id
    return _cached_private_key, _cached_key_pair_id


def is_configured() -> bool:
    key, key_id = _ensure_material()
    return bool(key and key_id and _sharing_url().startswith("https://"))


def _cf_b64(data: bytes) -> str:
    return (
        base64.b64encode(data)
        .decode("ascii")
        .replace("+", "-")
        .replace("=", "_")
        .replace("/", "~")
    )


def build_signed_cookies(*, expire_seconds: Optional[int] = None) -> Optional[dict[str, str]]:
    """Return CloudFront signed cookie name→value, or None if not configured."""
    private_key, key_pair_id = _ensure_material()
    sharing = _sharing_url()
    if not private_key or not key_pair_id or not sharing.startswith("https://"):
        return None

    if expire_seconds is None:
        try:
            try:
                from application import session_cookie
            except ImportError:
                import session_cookie

            expire_seconds = session_cookie.session_max_age_seconds()
        except Exception:
            expire_seconds = DEFAULT_COOKIE_MAX_AGE_SECONDS

    expire_at = int(time.time()) + max(60, int(expire_seconds))
    policy = {
        "Statement": [
            {
                "Resource": f"{sharing}/*",
                "Condition": {
                    "DateLessThan": {"AWS:EpochTime": expire_at},
                },
            }
        ]
    }
    policy_json = json.dumps(policy, separators=(",", ":")).encode("utf-8")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    # CloudFront trusted-signer signatures are fixed to RSA/SHA-1 by AWS.
    signature = private_key.sign(  # nosec B303
        policy_json, padding.PKCS1v15(), hashes.SHA1()  # nosec B303
    )

    return {
        COOKIE_POLICY: _cf_b64(policy_json),
        COOKIE_SIGNATURE: _cf_b64(signature),
        COOKIE_KEY_PAIR_ID: key_pair_id,
    }


def set_signed_cookies(response, *, secure: bool, max_age: Optional[int] = None) -> bool:
    """Attach CloudFront signed cookies to a FastAPI/Starlette Response."""
    cookies = build_signed_cookies(expire_seconds=max_age)
    if not cookies:
        return False
    if max_age is None:
        try:
            try:
                from application import session_cookie
            except ImportError:
                import session_cookie

            max_age = session_cookie.session_max_age_seconds()
        except Exception:
            max_age = DEFAULT_COOKIE_MAX_AGE_SECONDS

    for name, value in cookies.items():
        response.set_cookie(
            key=name,
            value=value,
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=max_age,
            path="/",
        )
    return True


def clear_signed_cookies(response, *, secure: bool) -> None:
    for name in ALL_COOKIE_NAMES:
        response.delete_cookie(
            key=name,
            path="/",
            samesite="lax",
            secure=secure,
        )
