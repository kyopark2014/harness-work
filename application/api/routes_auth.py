"""Session auth via Cognito USER_PASSWORD_AUTH + HMAC-signed cookies."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:
    from application import session_cookie
    from application import utils
except ImportError:
    import session_cookie  # type: ignore
    import utils  # type: ignore

logger = logging.getLogger("routes_auth")

_COGNITO_RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

router = APIRouter(prefix="/api/session", tags=["session"])

SESSION_COOKIE = "agent_user_id"


def _cookie_secure(request: Request) -> bool:
    proto = (
        request.headers.get("cloudfront-forwarded-proto")
        or request.headers.get("x-forwarded-proto")
        or request.url.scheme
        or ""
    ).lower()
    if proto == "https":
        return True
    host = (request.headers.get("host") or request.url.hostname or "").split(":")[0].lower()
    if host.endswith(".cloudfront.net"):
        return True
    try:
        sharing = (utils.load_config().get("sharing_url") or "").strip()
        parsed = urlparse(sharing)
        if parsed.scheme == "https" and (parsed.hostname or "").lower() == host:
            return True
    except Exception:
        pass
    return False


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    user_id: str
    knowledge_graph_enabled: bool = True


class SessionSettingsPatch(BaseModel):
    knowledge_graph_enabled: bool | None = None


def _cognito_settings() -> tuple[str, str, str]:
    config = utils.load_config()
    user_pool_id = (config.get("cognito_user_pool_id") or "").strip()
    client_id = (config.get("cognito_client_id") or "").strip()
    cognito_region = (
        config.get("cognito_region") or config.get("region") or "us-west-2"
    ).strip()
    if not user_pool_id or not client_id:
        raise HTTPException(
            status_code=503,
            detail="Cognito is not configured. Run installer.py to create the User Pool.",
        )
    return user_pool_id, client_id, cognito_region


def _authenticate_with_cognito(username: str, password: str) -> str:
    """Authenticate with Cognito and return the verified Username.

    Uses AccessToken → GetUser so the session is bound to a Cognito-confirmed
    identity, not the raw login form string.
    """
    _user_pool_id, client_id, cognito_region = _cognito_settings()
    client = boto3.client(
        "cognito-idp",
        region_name=cognito_region,
        config=_COGNITO_RETRY_CONFIG,
    )
    try:
        response = client.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
            },
        )
    except ClientError as e:
        err = e.response.get("Error", {}) or {}
        code = err.get("Code", "") or ""
        cognito_message = err.get("Message", "") or ""
        logger.warning(
            "Cognito auth failed for %s: %s (%s)",
            username,
            code,
            cognito_message or type(e).__name__,
        )
        if code in (
            "NotAuthorizedException",
            "UserNotFoundException",
            "UserNotConfirmedException",
            "PasswordResetRequiredException",
        ):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        if code == "InvalidParameterException":
            raise HTTPException(
                status_code=400, detail="Invalid authentication parameters"
            )
        raise HTTPException(status_code=502, detail="Authentication service error")

    challenge = response.get("ChallengeName")
    if challenge:
        raise HTTPException(
            status_code=403,
            detail=f"Additional authentication required: {challenge}",
        )
    auth_result = response.get("AuthenticationResult") or {}
    access_token = (auth_result.get("AccessToken") or "").strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Authentication failed")

    try:
        user = client.get_user(AccessToken=access_token)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.warning("Cognito GetUser failed after login: %s", code)
        raise HTTPException(status_code=401, detail="Authentication failed")

    verified = (user.get("Username") or "").strip()
    if not verified:
        raise HTTPException(status_code=401, detail="Authentication failed")
    return verified


def _set_session_cookie(response: Response, request: Request, user_id: str) -> None:
    token = session_cookie.sign_session(user_id)
    secure = _cookie_secure(request)
    max_age = session_cookie.session_max_age_seconds()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=max_age,
    )


def _kick_graph_job(user_id: str) -> None:
    """Fire-and-forget background graph extract (respects cooldown / running lock)."""
    if not utils.is_knowledge_graph_enabled(user_id):
        logger.info("Knowledge Graph disabled for %s — skip extract", user_id)
        return
    try:
        from application.graph_jobs import ensure_graph_job

        ensure_graph_job(user_id)
    except Exception:
        logger.exception("Failed to schedule graph job for %s", user_id)


def _session_response(user_id: str) -> SessionResponse:
    settings = utils.load_user_settings(user_id)
    return SessionResponse(
        user_id=user_id,
        knowledge_graph_enabled=bool(
            settings.get("knowledge_graph_enabled", True)
        ),
    )


def get_optional_user_id(request: Request) -> str | None:
    """Return verified user_id from the HMAC session cookie, or None."""
    return session_cookie.verify_session(request.cookies.get(SESSION_COOKIE) or "")


@router.post("/login", response_model=SessionResponse)
def login(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
    username = body.username.strip()
    password = body.password
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    user_id = _authenticate_with_cognito(username, password)
    _set_session_cookie(response, request, user_id)
    try:
        utils.ensure_user_graph_dir(user_id)
    except Exception:
        logger.exception("Failed to ensure graph dir for %s", user_id)
    _kick_graph_job(user_id)
    return _session_response(user_id)


@router.post("", response_model=SessionResponse, deprecated=True)
def set_session(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
    """Backward-compatible alias: requires Cognito username/password."""
    return login(body, request, response)


@router.get("", response_model=SessionResponse | None)
def get_session(request: Request) -> SessionResponse | None:
    user_id = get_optional_user_id(request)
    if not user_id:
        return None
    try:
        utils.ensure_user_graph_dir(user_id)
    except Exception:
        logger.exception("Failed to ensure graph dir for %s", user_id)
    _kick_graph_job(user_id)
    return _session_response(user_id)


@router.patch("/settings", response_model=SessionResponse)
def patch_session_settings(
    body: SessionSettingsPatch, request: Request
) -> SessionResponse:
    """Update per-user feature settings (e.g. Knowledge Graph toggle)."""
    user_id = require_user_id(request)
    updates: dict[str, bool] = {}
    if body.knowledge_graph_enabled is not None:
        updates["knowledge_graph_enabled"] = body.knowledge_graph_enabled
    if updates:
        utils.save_user_settings(user_id, **updates)
    if updates.get("knowledge_graph_enabled") is True:
        _kick_graph_job(user_id)
    return _session_response(user_id)


@router.delete("", status_code=204, response_model=None)
def clear_session(request: Request, response: Response) -> None:
    secure = _cookie_secure(request)
    response.delete_cookie(key=SESSION_COOKIE, samesite="lax", secure=secure)


def require_user_id(request: Request) -> str:
    user_id = get_optional_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="User session required")
    return user_id
