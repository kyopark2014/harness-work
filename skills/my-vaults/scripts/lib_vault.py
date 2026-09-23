#!/usr/bin/env python3
"""HTTP client helpers for ob-note vault API.

ob-note is a standalone site (default ``https://vault.my-agentic-ai.click``).
APIs live at ``/api/...`` (not the legacy ``/vault/api/...`` prefix).

Auth order (production AgentCore cannot read session-signing-key):
  1. VAULT_AGENT_TOKEN / Secrets Manager ``{project}/vault-agent-token``
     or ``ob-note/vault-agent-token`` / legacy ``ob-docs/vault-agent-token``
     → ``Authorization: VaultAgent v1.<payload>.<sig>``
  2. SESSION_SIGNING_KEY (local / app ECS) → Bearer session cookie token
  3. Loopback + no key → unauthenticated (ob-note ALLOW_LOCAL_AUTH_BYPASS)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
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
DEFAULT_OB_DOCS_URL = "https://vault.my-agentic-ai.click"


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
            "ob_docs_url": skill_cfg.get("ob_docs_url"),
            "region": skill_cfg.get("region"),
            "projectName": skill_cfg.get("project_name") or "ob-note",
        }

    candidates: list[Path] = []
    for key in ("OB_DOCS_ROOT", "WORKING_DIR", "APP_ROOT"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw) / "config.json")
            candidates.append(Path(raw) / "application" / "config.json")

    here = Path(__file__).resolve()
    # strands/skills/my-vaults/scripts → strands is parents[3]; cde-pilot is parents[5]
    for i in (3, 4, 5):
        try:
            root = here.parents[i]
            candidates.append(root / "config.json")
            candidates.append(root / "application" / "config.json")
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
            or "ob-note"
        )
        .strip()
        or "ob-note"
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
    """ob-note public base URL (never cde-pilot CloudFront SHARING_URL)."""
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

    # Do NOT fall back to SHARING_URL — in cde-pilot that is UI CloudFront, not vault.
    return DEFAULT_OB_DOCS_URL


def note_deep_link(path: str) -> Optional[str]:
    """Private deep link that opens a note after login (not a public /s/ share)."""
    cleaned = (path or "").replace("\\", "/").lstrip("/").strip()
    if not cleaned:
        return None
    if any(seg == ".." for seg in cleaned.split("/")):
        return None
    base = vault_base_url().rstrip("/")
    return f"{base}/?{urllib.parse.urlencode({'note': cleaned})}"


def attach_note_urls(payload: Any) -> Any:
    """Recursively add ``url`` (and ``from_url``) deep-link fields for ``*.md`` paths."""
    if isinstance(payload, list):
        return [attach_note_urls(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    out: dict[str, Any] = {k: attach_note_urls(v) for k, v in payload.items()}

    path_val = out.get("path")
    if isinstance(path_val, str) and path_val.strip().lower().endswith(".md"):
        url = note_deep_link(path_val.strip())
        if url:
            out["url"] = url

    to_val = out.get("to") or out.get("to_path")
    if isinstance(to_val, str) and to_val.strip().lower().endswith(".md"):
        url = note_deep_link(to_val.strip())
        if url:
            out["url"] = url

    from_val = out.get("from") or out.get("from_path")
    if isinstance(from_val, str) and from_val.strip().lower().endswith(".md"):
        url = note_deep_link(from_val.strip())
        if url:
            out["from_url"] = url

    return out


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
    """Load HMAC key shared with ob-note VaultAgent auth.

    Tries (in order):
      1. VAULT_AGENT_TOKEN env
      2. OB_DOCS_VAULT_AGENT_SECRET override
      3. ``{project}/vault-agent-token`` (Runtime IAM already allows)
      4. ``ob-note/vault-agent-token`` then legacy ``ob-docs/vault-agent-token``
      5. App project name extras (cde-pilot / gsolution etc.)
    """
    errors: list[str] = []
    env = (os.environ.get("VAULT_AGENT_TOKEN") or "").strip()
    if env:
        return env.encode("utf-8"), errors

    secret_ids: list[str] = []
    override = (os.environ.get("OB_DOCS_VAULT_AGENT_SECRET") or "").strip()
    if override:
        secret_ids.append(override)
    secret_ids.append(f"{_project_name()}/vault-agent-token")
    secret_ids.append("ob-note/vault-agent-token")
    secret_ids.append("ob-docs/vault-agent-token")  # legacy fallback
    for extra in (
        (os.environ.get("PROJECT_NAME") or "").strip(),
        (os.environ.get("SHARED_PROJECT_NAME") or "").strip(),
        "gsolution",
        "cde-pilot",
    ):
        if extra:
            secret_ids.append(f"{extra}/vault-agent-token")

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
        "(Secrets Manager `ob-note/vault-agent-token` or "
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
        raise RuntimeError(f"Failed to reach ob-note at {base}: {exc}") from exc


def api_upload(
    vault_path: str,
    data: bytes,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    user_id: Optional[str] = None,
    timeout: float = 120.0,
) -> Any:
    """POST multipart ``/api/files/upload`` (images / binary beside a note)."""
    import mimetypes

    uid = resolve_user_id(user_id)
    cleaned = (vault_path or "").replace("\\", "/").lstrip("/").strip()
    if not cleaned or ".." in cleaned.split("/"):
        raise ValueError(f"invalid vault path: {vault_path!r}")

    name = filename or Path(cleaned).name or "upload.bin"
    ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = f"----VaultUpload{int(time.time() * 1000)}{os.getpid()}"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="path"\r\n\r\n'
            f"{cleaned}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )

    headers = _auth_headers(uid, require_auth=True)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    url = f"{vault_base_url()}/api/files/upload"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {"ok": True, "path": cleaned}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} POST /api/files/upload: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach ob-note at {vault_base_url()}: {exc}") from exc


# --- markdown image embed helpers -------------------------------------------

_MD_IMG_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*(?:<([^>\n]+)>|([^)\n]+))\s*\)",
)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}


def note_dir(note_path: str) -> str:
    cleaned = (note_path or "").replace("\\", "/").lstrip("/").strip()
    if "/" not in cleaned:
        return ""
    return cleaned.rsplit("/", 1)[0]


def safe_image_filename(name: str, *, used: Optional[set[str]] = None) -> str:
    """Basename safe for vault + unique within a note folder."""
    raw = (name or "image.png").replace("\\", "/").split("/")[-1].strip()
    raw = re.sub(r"[^\w.\-]+", "_", raw, flags=re.UNICODE).strip("._") or "image.png"
    stem = Path(raw).stem or "image"
    suffix = Path(raw).suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        suffix = ".png"
        raw = f"{stem}{suffix}"
    if used is None:
        return raw
    candidate = raw
    n = 2
    while candidate.lower() in used:
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    used.add(candidate.lower())
    return candidate


def extract_md_images(content: str) -> list[tuple[str, str, str]]:
    """Return list of (full_match, alt, src) for markdown images."""
    out: list[tuple[str, str, str]] = []
    for m in _MD_IMG_RE.finditer(content or ""):
        alt = m.group(1) or ""
        src = (m.group(2) or m.group(3) or "").strip()
        if src:
            out.append((m.group(0), alt, src))
    return out


def _artifacts_dirs() -> list[Path]:
    dirs: list[Path] = []
    for key in ("ARTIFACTS_DIR", "ARTIFACT_DIR"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            dirs.append(Path(raw))
    cwd = Path.cwd()
    dirs.extend([cwd, cwd / "artifacts"])
    # common runtime layout when cwd is already artifacts/
    if cwd.name == "artifacts":
        dirs.append(cwd)
    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve())
        except Exception:
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return unique


def _s3_bucket() -> Optional[str]:
    skill = load_skill_config()
    cfg = load_app_config()
    return (
        (os.environ.get("S3_BUCKET") or "").strip()
        or (skill.get("s3_bucket") or "").strip()
        or (cfg.get("s3_bucket") or "").strip()
        or None
    )


def _try_read_local(candidates: list[Path]) -> Optional[tuple[bytes, str]]:
    for path in candidates:
        try:
            if path.is_file():
                return path.read_bytes(), path.name
        except OSError:
            continue
    return None


def _try_s3_get(key: str) -> Optional[tuple[bytes, str]]:
    bucket = _s3_bucket()
    if not bucket or not key:
        return None
    try:
        import boto3
    except Exception:
        return None
    try:
        client = boto3.client("s3", region_name=_region())
        resp = client.get_object(Bucket=bucket, Key=key)
        data = resp["Body"].read()
        if data:
            return data, Path(key).name
    except Exception:
        return None
    return None


def _http_get_bytes(url: str, *, timeout: float = 60.0) -> Optional[tuple[bytes, str]]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "my-vaults-embed/1.0", "Accept": "image/*,*/*"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if not data:
                return None
            name = Path(urllib.parse.urlparse(url).path).name or "image.bin"
            return data, name
    except Exception:
        return None


def resolve_image_source(
    src: str,
    *,
    local_map: Optional[dict[str, str]] = None,
) -> tuple[bytes, str]:
    """Load image bytes from local file, ARTIFACTS_DIR, S3, or HTTP.

    CloudFront ``/artifacts/*`` URLs require signed cookies; prefer local/S3.
    """
    cleaned = (src or "").strip().strip("<>").strip()
    if not cleaned:
        raise ValueError("empty image src")

    if local_map:
        mapped = local_map.get(cleaned) or local_map.get(Path(cleaned).name)
        if mapped:
            path = Path(mapped).expanduser()
            if path.is_file():
                return path.read_bytes(), path.name

    # Already a vault-relative / bare filename — skip remote fetch
    if not (
        cleaned.startswith("http://")
        or cleaned.startswith("https://")
        or cleaned.startswith("data:")
        or cleaned.startswith("/")
        or Path(cleaned).is_absolute()
    ):
        # relative path next to note — caller should not re-upload usually
        local = _try_read_local([Path(cleaned), *_artifacts_dirs_join(cleaned)])
        if local:
            return local
        raise FileNotFoundError(f"local image not found: {cleaned}")

    if cleaned.startswith("data:"):
        # data:image/png;base64,...
        try:
            header, b64 = cleaned.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "gif" in header:
                ext = ".gif"
            elif "webp" in header:
                ext = ".webp"
            elif "svg" in header:
                ext = ".svg"
            return base64.b64decode(b64), f"embedded{ext}"
        except Exception as exc:
            raise ValueError(f"invalid data: URL: {exc}") from exc

    # Absolute / relative filesystem path
    as_path = Path(cleaned).expanduser()
    if as_path.is_file():
        return as_path.read_bytes(), as_path.name

    basename = Path(urllib.parse.urlparse(cleaned).path).name or Path(cleaned).name
    # Prefer local artifacts (agent cwd / ARTIFACTS_DIR)
    local = _try_read_local(_artifacts_dirs_join(basename) + _artifacts_dirs_join(cleaned))
    if local:
        return local

    # S3: artifacts/{user}/{file} from CloudFront/S3 URL path
    parsed = urllib.parse.urlparse(cleaned)
    key = parsed.path.lstrip("/") if parsed.scheme in ("http", "https") else ""
    if key.startswith("artifacts/") or key.startswith("images/") or key.startswith("docs/"):
        got = _try_s3_get(key)
        if got:
            return got
        # also try bare basename under artifacts/
        if basename:
            for prefix in ("artifacts", "images"):
                got = _try_s3_get(f"{prefix}/{basename}")
                if got:
                    return got

    if parsed.scheme in ("http", "https"):
        got = _http_get_bytes(cleaned)
        if got:
            return got
        raise FileNotFoundError(
            f"could not fetch image (CloudFront may need signed cookies; "
            f"place file in ARTIFACTS_DIR or pass --map): {cleaned}"
        )

    raise FileNotFoundError(f"image not found: {cleaned}")


def _artifacts_dirs_join(rel: str) -> list[Path]:
    rel_clean = rel.replace("\\", "/").lstrip("/")
    # strip artifacts/user/ prefix for basename search
    parts = [p for p in rel_clean.split("/") if p and p != ".."]
    names = []
    if parts:
        names.append("/".join(parts))
        names.append(parts[-1])
        if len(parts) >= 2:
            names.append("/".join(parts[-2:]))
    out: list[Path] = []
    for d in _artifacts_dirs():
        for n in names:
            out.append(d / n)
    return out


def embed_images(
    note_path: str,
    content: str,
    *,
    user_id: Optional[str] = None,
    local_map: Optional[dict[str, str]] = None,
    skip_relative: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """Download/copy remote images into the note folder; rewrite to relative links.

    Returns (new_content, embed_results).
    """
    folder = note_dir(note_path)
    results: list[dict[str, Any]] = []
    used_names: set[str] = set()
    # Preserve order; rewrite from end so offsets stay valid
    matches = list(_MD_IMG_RE.finditer(content or ""))
    if not matches:
        return content, results

    new_content = content
    # Process unique srcs once
    src_to_rel: dict[str, str] = {}

    for m in matches:
        src = (m.group(2) or m.group(3) or "").strip()
        if not src:
            continue
        if src in src_to_rel:
            continue
        is_remote = src.startswith(("http://", "https://", "data:"))
        is_abs = src.startswith("/") or Path(src).is_absolute()
        if skip_relative and not is_remote and not is_abs:
            # already a vault-relative / same-folder link
            results.append({"src": src, "skipped": True, "reason": "already_relative"})
            continue

        try:
            data, original_name = resolve_image_source(src, local_map=local_map)
        except Exception as exc:
            results.append({"src": src, "ok": False, "error": str(exc)})
            continue

        fname = safe_image_filename(original_name or Path(src).name, used=used_names)
        vault_img = f"{folder}/{fname}" if folder else fname
        try:
            upload_result = api_upload(
                vault_img,
                data,
                filename=fname,
                user_id=user_id,
            )
        except Exception as exc:
            results.append(
                {
                    "src": src,
                    "ok": False,
                    "vault_path": vault_img,
                    "error": str(exc),
                }
            )
            continue

        src_to_rel[src] = fname
        results.append(
            {
                "src": src,
                "ok": True,
                "vault_path": vault_img,
                "link": fname,
                "size": len(data),
                "upload": upload_result,
            }
        )

    if not src_to_rel:
        return new_content, results

    def _repl(m: re.Match[str]) -> str:
        alt = m.group(1) or ""
        src = (m.group(2) or m.group(3) or "").strip()
        rel = src_to_rel.get(src)
        if not rel:
            return m.group(0)
        if " " in rel or "(" in rel or ")" in rel:
            return f"![{alt}](<{rel}>)"
        return f"![{alt}]({rel})"

    new_content = _MD_IMG_RE.sub(_repl, new_content)
    return new_content, results


def print_json(payload: Any, *, with_urls: bool = True) -> None:
    data = attach_note_urls(payload) if with_urls else payload
    print(json.dumps(data, ensure_ascii=False, indent=2))
