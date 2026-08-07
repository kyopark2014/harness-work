"""HMAC-signed session cookies for Web UI auth.

Cookie value is opaque to clients: `v1.<payload_b64>.<sig_b64>`.
Plain `user_id` cookies are rejected so values cannot be forged by editing the cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("session_cookie")

COOKIE_VERSION = "v1"
DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
# 32 bytes = 256 bits of entropy for the HMAC signing key, matching the
# SHA-256 output size used to sign cookies (see _sign()).
SIGNING_KEY_BYTES = 32
_ENV_KEY = "SESSION_SIGNING_KEY"
_LOCAL_KEY_FILE = Path(__file__).resolve().parent / "data" / ".session_signing_key"

_key_lock = threading.Lock()
_cached_key: Optional[bytes] = None


def session_max_age_seconds() -> int:
    raw = (os.environ.get("SESSION_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_AGE_SECONDS


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _project_name() -> str:
    try:
        try:
            from application import utils
        except ImportError:
            import utils

        cfg = utils.load_config()
        name = (cfg.get("projectName") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return (os.environ.get("TASK_DB_PROJECT") or "harness-work").strip() or "harness-work"


def _secret_name() -> str:
    return f"{_project_name()}/session-signing-key"


def _load_key_from_secrets_manager() -> Optional[bytes]:
    try:
        import boto3

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=_secret_name())
        secret = (response.get("SecretString") or "").strip()
        if secret:
            return secret.encode("utf-8")
    except Exception as e:
        # Logs only the exception, never the key material.
        logger.debug("Session signing key not loaded from Secrets Manager: %s", e)  # nosemgrep: python.lang.security.audit.logging.python-logger-credential-disclosure
    return None


def _load_or_create_local_key() -> bytes:
    _LOCAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCAL_KEY_FILE.exists():
        try:
            existing = _LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                "Failed to read local session signing key at %s: %s",
                _LOCAL_KEY_FILE,
                e,
            )
            existing = ""
        if existing:
            return existing.encode("utf-8")
    value = secrets.token_urlsafe(SIGNING_KEY_BYTES)
    try:
        _LOCAL_KEY_FILE.write_text(value + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning(
            "Failed to persist local session signing key at %s: %s",
            _LOCAL_KEY_FILE,
            e,
        )
        return value.encode("utf-8")
    try:
        os.chmod(_LOCAL_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info("Created local session signing key at %s", _LOCAL_KEY_FILE)
    return value.encode("utf-8")


def get_signing_key() -> bytes:
    """Resolve HMAC key: env → Secrets Manager → local file (dev)."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    with _key_lock:
        if _cached_key is not None:
            return _cached_key
        env_key = (os.environ.get(_ENV_KEY) or "").strip()
        if env_key:
            _cached_key = env_key.encode("utf-8")
            return _cached_key
        sm_key = _load_key_from_secrets_manager()
        if sm_key:
            _cached_key = sm_key
            return _cached_key
        _cached_key = _load_or_create_local_key()
        return _cached_key


def reset_signing_key_cache() -> None:
    """Test helper: drop cached key so the next call re-resolves."""
    global _cached_key
    with _key_lock:
        _cached_key = None


def sign_session(user_id: str, *, max_age_seconds: Optional[int] = None) -> str:
    """Return an HMAC-signed cookie value for user_id."""
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    age = session_max_age_seconds() if max_age_seconds is None else max_age_seconds
    exp = int(time.time()) + int(age)
    payload = json.dumps({"uid": uid, "exp": exp}, separators=(",", ":"), ensure_ascii=False)
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{COOKIE_VERSION}.{payload_b64}.{_b64encode(sig)}"


def verify_session(cookie_value: str) -> Optional[str]:
    """Return user_id if the signed cookie is valid; otherwise None."""
    raw = (cookie_value or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION:
        # Reject legacy plain-text user_id cookies.
        return None
    _version, payload_b64, sig_b64 = parts
    try:
        expected = hmac.new(
            get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        provided = _b64decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    uid = (payload.get("uid") or "").strip()
    exp = payload.get("exp")
    if not uid or not isinstance(exp, int):
        return None
    if exp < int(time.time()):
        return None
    return uid
