#!/usr/bin/env python3
"""HTTP client helpers for ob-docs vault API.

Auth order (production AgentCore cannot read session-signing-key):
  1. VAULT_AGENT_TOKEN / Secrets Manager ``{project}/vault-agent-token``
     → ``Authorization: VaultAgent v1.<payload>.<sig>``
  2. SESSION_SIGNING_KEY (local / app ECS) → Bearer session cookie token
  3. Loopback + no key → unauthenticated (ob-docs ALLOW_LOCAL_AUTH_BYPASS)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

COOKIE_NAME = "agent_user_id"
COOKIE_VERSION = "v1"
VAULT_AGENT_SCHEME = "VaultAgent"
VAULT_AGENT_VERSION = "v1"
DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
VAULT_AGENT_MAX_AGE_SECONDS = 60 * 60


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_skill_config() -> dict[str, Any]:
    """Sidecar config shipped with the skill (code interpreter env fallback)."""
    here = Path(__file__).resolve().parent
    for path in (here.parent / "config.json", here / "config.json"):
        if path.is_file():
            return _load_json(path)
    return {}


def load_app_config() -> dict[str, Any]:
    env_json = (os.environ.get("APP_CONFIG_JSON") or "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    skill_cfg = load_skill_config()
    if skill_cfg:
        return {
            "s3_bucket": skill_cfg.get("s3_bucket"),
            "sharing_url": skill_cfg.get("sharing_url") or skill_cfg.get("ob_docs_url"),
            "region": skill_cfg.get("region"),
            "projectName": skill_cfg.get("project_name") or "ob-docs",
        }

    candidates: list[Path] = []
    for key in ("OB_DOCS_ROOT", "WORKING_DIR", "APP_ROOT"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw) / "config.json")
            candidates.append(Path(raw) / "application" / "config.json")

    here = Path(__file__).resolve()
    # skills/use-vault/scripts → ob-docs root is parents[3]
    try:
        ob_docs_root = here.parents[3]
        candidates.append(ob_docs_root / "config.json")
    except IndexError:
        pass
    candidates.extend(
        [
            Path.cwd() / "config.json",
            Path.cwd() / "application" / "config.json",
        ]
    )
    for path in candidates:
        if path.is_file():
            return _load_json(path)
    return {}


def _project_name(cfg: Optional[dict[str, Any]] = None) -> str:
    """Secrets Manager prefix for vault-agent-token."""
    cfg = cfg if cfg is not None else load_app_config()
    skill = load_skill_config()
    return (
        (
            os.environ.get("PROJECT_NAME")
            or os.environ.get("SHARED_PROJECT_NAME")
            or skill.get("project_name")
            or cfg.get("projectName")
            or "ob-docs"
        )
        .strip()
        or "ob-docs"
    )


def _region(cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_app_config()
    skill = load_skill_config()
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or skill.get("region")
        or (cfg.get("region") or "us-west-2")
    )


def vault_base_url() -> str:
    """ob-docs public base URL (never harness CloudFront SHARING_URL)."""
    explicit = (
        (os.environ.get("OB_DOCS_URL") or "").strip()
        or (os.environ.get("VAULT_API_URL") or "").strip()
    )
    if explicit:
        return explicit.rstrip("/")

    skill = load_skill_config()
    skill_url = (skill.get("ob_docs_url") or "").strip()
    if skill_url:
        return skill_url.rstrip("/")

    cfg = load_app_config()
    dedicated = (cfg.get("ob_docs_url") or cfg.get("vault_url") or "").strip()
    if dedicated:
        return dedicated.rstrip("/")

    # Do NOT fall back to SHARING_URL — in harness that is UI CloudFront, not vault.
    return "https://vault.my-agentic-ai.click"


def resolve_user_id(cli_user_id: Optional[str] = None) -> str:
    for candidate in (
        cli_user_id,
        os.environ.get("USER_ID"),
        os.environ.get("CURRENT_USER_ID"),
        os.environ.get("AGENT_USER_ID"),
        os.environ.get("ACTOR_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return "local-dev"


def _session_max_age() -> int:
    raw = (os.environ.get("SESSION_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_AGE_SECONDS


def _get_secret_string(secret_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (secret, error_message)."""
    try:
        import boto3
    except Exception as exc:
        return None, f"boto3 unavailable: {exc}"
    try:
        client = boto3.client("secretsmanager", region_name=_region())
        response = client.get_secret_value(SecretId=secret_id)
        secret = (response.get("SecretString") or "").strip()
        if secret:
            return secret, None
        return None, f"empty secret: {secret_id}"
    except Exception as exc:
        return None, f"{secret_id}: {exc}"


def _vault_agent_token() -> tuple[Optional[bytes], list[str]]:
    """Load HMAC key shared with ob-docs VaultAgent auth.

    Tries (in order):
      1. VAULT_AGENT_TOKEN env
      2. OB_DOCS_VAULT_AGENT_SECRET override
      3. ``ob-docs/vault-agent-token``
      4. ``{project}/vault-agent-token`` (skill/app projectName)
    """
    errors: list[str] = []
    env = (os.environ.get("VAULT_AGENT_TOKEN") or "").strip()
    if env:
        return env.encode("utf-8"), errors

    secret_ids: list[str] = []
    override = (os.environ.get("OB_DOCS_VAULT_AGENT_SECRET") or "").strip()
    if override:
        secret_ids.append(override)
    secret_ids.append("ob-docs/vault-agent-token")
    project = _project_name()
    if project and project != "ob-docs":
        secret_ids.append(f"{project}/vault-agent-token")

    seen: set[str] = set()
    for secret_id in secret_ids:
        if secret_id in seen:
            continue
        seen.add(secret_id)
        value, err = _get_secret_string(secret_id)
        if value:
            return value.encode("utf-8"), errors
        if err:
            errors.append(err)
    return None, errors


def _session_signing_key() -> tuple[Optional[bytes], list[str]]:
    errors: list[str] = []
    env_key = (os.environ.get("SESSION_SIGNING_KEY") or "").strip()
    if env_key:
        return env_key.encode("utf-8"), errors

    secret_id = f"{_project_name()}/session-signing-key"
    value, err = _get_secret_string(secret_id)
    if value:
        return value.encode("utf-8"), errors
    if err:
        errors.append(err)

    for path in (
        Path.cwd() / "application" / "data" / ".session_signing_key",
        Path.cwd() / "data" / ".session_signing_key",
    ):
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.encode("utf-8"), errors
    return None, errors


def _sign_vault_agent(user_id: str, token: bytes) -> str:
    exp = int(time.time()) + VAULT_AGENT_MAX_AGE_SECONDS
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(token, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{VAULT_AGENT_VERSION}.{payload_b64}.{_b64encode(sig)}"


def _sign_session(user_id: str, key: bytes) -> str:
    exp = int(time.time()) + _session_max_age()
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{COOKIE_VERSION}.{payload_b64}.{_b64encode(sig)}"


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def _auth_headers(user_id: str, *, require_auth: bool = True) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    errors: list[str] = []

    agent_token, agent_errors = _vault_agent_token()
    errors.extend(agent_errors)
    if agent_token:
        cred = _sign_vault_agent(user_id, agent_token)
        headers["Authorization"] = f"{VAULT_AGENT_SCHEME} {cred}"
        return headers

    session_key, session_errors = _session_signing_key()
    errors.extend(session_errors)
    if session_key:
        token = _sign_session(user_id, session_key)
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Vault-Session"] = token
        headers["Cookie"] = f"{COOKIE_NAME}={token}"
        return headers

    if not require_auth or _is_loopback(vault_base_url()):
        return headers

    detail = "; ".join(errors) if errors else "no credentials resolved"
    raise RuntimeError(
        "Vault auth unavailable. Need VAULT_AGENT_TOKEN "
        "(Secrets Manager `ob-docs/vault-agent-token` or "
        f"`{_project_name()}/vault-agent-token`) for AgentCore, "
        f"or SESSION_SIGNING_KEY for local/app. Details: {detail}"
    )


def api_request(
    method: str,
    path: str,
    *,
    user_id: Optional[str] = None,
    query: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Any:
    uid = resolve_user_id(user_id)
    base = vault_base_url()
    url = f"{base}{path}"
    if query:
        filtered = {k: v for k, v in query.items() if v is not None and v != ""}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"

    # health is public
    require_auth = path.rstrip("/") != "/api/health"
    data = None
    headers = _auth_headers(uid, require_auth=require_auth)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True, "status": resp.status}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} {method.upper()} {path}: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach ob-docs at {base}: {exc}") from exc


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
