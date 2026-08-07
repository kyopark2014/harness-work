#!/usr/bin/env python3
"""Register an additional Cognito user for Web UI login.

Reads User Pool / App Client settings from application/config.json (written by
installer.py), creates the user with a permanent password, then verifies login
via USER_PASSWORD_AUTH (same flow as application/api/routes_auth.py).

Usage:
  python add_user.py
  python add_user.py --username user01
  python add_user.py --username user01 --password 'YourPassword1'
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("add_user")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "application" / "config.json"


def _cognito_password_valid(password: str) -> Optional[str]:
    """Return an error message if password does not meet Cognito policy, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return "Password must include at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must include at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must include at least one number"
    return None


def load_cognito_config(config_path: Path) -> Dict[str, str]:
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Config not found: {config_path}. Run installer.py first."
        )
    with config_path.open(encoding="utf-8") as f:
        config: Dict[str, Any] = json.load(f)

    user_pool_id = (config.get("cognito_user_pool_id") or "").strip()
    client_id = (config.get("cognito_client_id") or "").strip()
    region = (
        config.get("cognito_region") or config.get("region") or "us-west-2"
    ).strip()
    if not user_pool_id or not client_id:
        raise ValueError(
            "cognito_user_pool_id / cognito_client_id missing in config. "
            "Run installer.py to create the Cognito User Pool."
        )
    return {
        "cognito_user_pool_id": user_pool_id,
        "cognito_client_id": client_id,
        "cognito_region": region,
        "project_name": (config.get("projectName") or "").strip(),
        "sharing_url": (config.get("sharing_url") or "").strip(),
    }


def prompt_username(default: Optional[str] = None) -> str:
    while True:
        prompt = "Username: "
        if default:
            prompt = f"Username [{default}]: "
        value = (input(prompt).strip() or (default or "")).strip()
        if value:
            return value
        logger.warning("  Username is required.")


def prompt_password(username: str) -> str:
    logger.info(
        "  Password policy: min 8 chars, uppercase, lowercase, number "
        "(symbols optional)"
    )
    while True:
        password = getpass.getpass(f"Enter password for '{username}': ")
        error = _cognito_password_valid(password)
        if error:
            logger.warning("  %s. Try again.", error)
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            logger.warning("  Passwords do not match. Try again.")
            continue
        return password


def user_exists(client, user_pool_id: str, username: str) -> bool:
    try:
        client.admin_get_user(UserPoolId=user_pool_id, Username=username)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "UserNotFoundException":
            return False
        raise


def create_user(client, user_pool_id: str, username: str, password: str) -> None:
    """Create user and set a permanent password (no forced change on first login)."""
    client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        TemporaryPassword=password,
        MessageAction="SUPPRESS",
    )
    client.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def test_login(
    client, client_id: str, username: str, password: str
) -> Tuple[bool, str]:
    """Verify USER_PASSWORD_AUTH works the same way as Web UI login.

    Returns (success, detail_message).
    """
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
        code = e.response.get("Error", {}).get("Code", "") or type(e).__name__
        message = e.response.get("Error", {}).get("Message") or "unknown"
        return False, f"InitiateAuth failed: {code} — {message}"

    challenge = response.get("ChallengeName")
    if challenge:
        return False, f"Unexpected auth challenge: {challenge}"

    auth_result = response.get("AuthenticationResult") or {}
    access_token = (auth_result.get("AccessToken") or "").strip()
    if not access_token:
        return False, "AuthenticationResult has no AccessToken"

    try:
        user = client.get_user(AccessToken=access_token)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "") or type(e).__name__
        message = e.response.get("Error", {}).get("Message") or "unknown"
        return False, f"GetUser failed: {code} — {message}"

    verified = (user.get("Username") or "").strip()
    status = (user.get("UserStatus") or "").strip() or "UNKNOWN"
    if verified != username:
        return (
            False,
            f"Login ok but Cognito Username mismatch: expected={username!r}, got={verified!r}",
        )
    return True, f"Login OK (Username={verified}, UserStatus={status})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register a Cognito user for harness-work Web UI login.",
    )
    parser.add_argument(
        "--username",
        "-u",
        help="Cognito username (prompted if omitted)",
    )
    parser.add_argument(
        "--password",
        "-p",
        help="Permanent password (prompted securely if omitted)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to application config.json (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--skip-login-test",
        action="store_true",
        help="Skip USER_PASSWORD_AUTH login verification after create",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cognito = load_cognito_config(Path(args.config))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        logger.error("%s", e)
        return 1

    user_pool_id = cognito["cognito_user_pool_id"]
    client_id = cognito["cognito_client_id"]
    region = cognito["cognito_region"]

    logger.info("Cognito user registration")
    logger.info("  Region:     %s", region)
    if cognito.get("project_name"):
        logger.info("  Project:    %s", cognito["project_name"])

    username = (args.username or "").strip() or prompt_username()
    if args.password:
        password = args.password
        error = _cognito_password_valid(password)
        if error:
            logger.error("%s", error)
            return 1
    else:
        password = prompt_password(username)

    try:
        client = boto3.client("cognito-idp", region_name=region)
    except NoCredentialsError:
        logger.error("AWS credentials not found. Configure credentials and retry.")
        return 1

    try:
        if user_exists(client, user_pool_id, username):
            logger.error("User already exists: %s", username)
            return 1

        create_user(client, user_pool_id, username, password)
        logger.info("  ✓ Cognito user created: %s", username)

        if args.skip_login_test:
            logger.info("  Skipping login test (--skip-login-test)")
            login_ok = True
            login_detail = "skipped"
        else:
            logger.info("  Testing login (USER_PASSWORD_AUTH)...")
            login_ok, login_detail = test_login(client, client_id, username, password)
            if login_ok:
                logger.info("  ✓ %s", login_detail)
            else:
                logger.error("  ✗ %s", login_detail)

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "") or type(e).__name__
        message = e.response.get("Error", {}).get("Message") or "unknown"
        logger.error("Cognito error: %s — %s", code, message)
        return 1
    except Exception as e:
        logger.error("Unexpected error: %s", type(e).__name__)
        return 1

    logger.info("")
    logger.info("Result")
    logger.info("  Username:  %s", username)
    logger.info("  Created:   yes")
    logger.info("  Login:     %s", "OK" if login_ok else "FAILED")
    if not login_ok:
        logger.info("  Detail:    %s", login_detail)
    sharing_url = cognito.get("sharing_url")
    if sharing_url:
        logger.info("  Web UI:    %s", sharing_url)
        logger.info("  Sign in with the username/password you just set.")

    return 0 if login_ok else 2


if __name__ == "__main__":
    sys.exit(main())
