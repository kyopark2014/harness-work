"""Session auth — local User ID plus optional Google OAuth."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:
    from application import utils
except ImportError:
    import utils  # type: ignore

logger = logging.getLogger("routes_auth")

router = APIRouter(prefix="/api/session", tags=["session"])

SESSION_COOKIE = "agent_user_id"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_MAX_PLAIN_USER_ID_LEN = 128


class SessionRequest(BaseModel):
    credential: str | None = Field(
        default=None, description="Google ID Token (JWT)"
    )
    access_token: str | None = Field(
        default=None, description="Google OAuth access token"
    )
    user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Local-only user id when auth bypass is enabled",
    )


class SessionResponse(BaseModel):
    user_id: str
    name: str | None = None
    picture: str | None = None
    llm_gateway_ready: bool = False
    knowledge_graph_enabled: bool = False


def _google_client_id() -> str:
    cfg = utils.load_config()
    return (cfg.get("google_client_id") or "").strip()


def _env_bypass_flag() -> bool:
    return os.environ.get("ALLOW_LOCAL_AUTH_BYPASS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_loopback_request(request: Request) -> bool:
    host = (request.headers.get("host") or "").split("%")[0]
    hostname = host.split(":")[0].strip().lower().strip("[]")
    return hostname in {"localhost", "127.0.0.1", "::1"}


def local_auth_bypass_enabled(request: Request) -> bool:
    if _env_bypass_flag() or is_loopback_request(request):
        return True
    return not bool(_google_client_id())


def verify_google_token(token: str, client_id: str) -> dict:
    url = f"{TOKENINFO_URL}?id_token={urllib.parse.quote(token)}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            idinfo = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Token verification failed ({e.code}): {body}") from e
    except Exception as e:
        raise ValueError(f"Token verification request failed: {e}") from e

    if idinfo.get("aud") != client_id:
        raise ValueError(f"Invalid audience: {idinfo.get('aud')}")
    email = (idinfo.get("email") or "").strip()
    if not email:
        raise ValueError("Google token missing email")
    return idinfo


def verify_google_access_token(token: str, client_id: str) -> dict:
    info_url = f"{TOKENINFO_URL}?access_token={urllib.parse.quote(token)}"
    req = urllib.request.Request(info_url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            idinfo = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Token verification failed ({e.code}): {body}") from e
    except Exception as e:
        raise ValueError(f"Token verification request failed: {e}") from e

    aud = idinfo.get("aud") or idinfo.get("azp")
    if aud != client_id:
        raise ValueError(f"Invalid audience: {aud}")
    email = (idinfo.get("email") or "").strip()
    if not email:
        raise ValueError("Google token missing email")
    return idinfo


def _set_user_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=user_id,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )


def resolve_cookie_user_id(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) > _MAX_PLAIN_USER_ID_LEN:
        logger.warning("Ignoring oversized session cookie (%d chars)", len(value))
        return None
    return value


def get_optional_user_id(request: Request) -> str | None:
    return resolve_cookie_user_id(request.cookies.get(SESSION_COOKIE))


def _session_response(
    user_id: str,
    *,
    name: str | None = None,
    picture: str | None = None,
) -> SessionResponse:
    return SessionResponse(
        user_id=user_id,
        name=name,
        picture=picture,
        llm_gateway_ready=False,
        knowledge_graph_enabled=False,
    )


@router.post("", response_model=SessionResponse)
def set_session(
    body: SessionRequest, request: Request, response: Response
) -> SessionResponse:
    credential = (body.credential or "").strip()
    access_token = (body.access_token or "").strip()
    local_user_id = (body.user_id or "").strip()

    if credential or access_token:
        client_id = _google_client_id()
        if not client_id:
            raise HTTPException(
                status_code=500, detail="google_client_id is not configured"
            )
        try:
            if credential:
                idinfo = verify_google_token(credential, client_id)
            else:
                idinfo = verify_google_access_token(access_token, client_id)
        except ValueError as e:
            logger.warning("Google login rejected: %s", e)
            raise HTTPException(
                status_code=401, detail="Invalid Google credential"
            ) from e

        user_id = idinfo["email"].strip()
        _set_user_cookie(response, user_id)
        logger.info("Google login success: %s", user_id)
        return _session_response(
            user_id,
            name=(idinfo.get("name") or None),
            picture=(idinfo.get("picture") or None),
        )

    if local_user_id:
        if not local_auth_bypass_enabled(request):
            raise HTTPException(
                status_code=403,
                detail="Local auth bypass is disabled",
            )
        _set_user_cookie(response, local_user_id)
        logger.info("Local auth bypass login: %s", local_user_id)
        return _session_response(local_user_id)

    raise HTTPException(
        status_code=400, detail="credential, access_token, or user_id is required"
    )


@router.get("", response_model=SessionResponse | None)
def get_session(request: Request) -> SessionResponse | None:
    user_id = get_optional_user_id(request)
    if not user_id:
        return None
    return _session_response(user_id)


@router.delete("", status_code=204, response_model=None)
def clear_session(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, samesite="lax")


def require_user_id(request: Request) -> str:
    user_id = get_optional_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="User session required")
    return user_id
