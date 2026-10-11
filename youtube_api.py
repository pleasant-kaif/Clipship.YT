from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import uuid

from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException

from quota_budget import (
    APP_DATA_DIR,
    PRIORITY_COSTS,
    QuotaBudget,
    connect,
    finish_api_task,
    queue_api_task,
    set_settings,
)


APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENT_SECRET_PATH = APP_DATA_DIR / "youtube_client_secret.json"
TOKEN_PATH = APP_DATA_DIR / "youtube_token.json"
CALLBACK_URL = "http://127.0.0.1:5000/api/youtube/callback"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
logger = logging.getLogger(__name__)
budget = QuotaBudget()


def save_uploaded_client_secret(file_bytes: bytes) -> None:
    try:
        secret = json.loads(file_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Upload a valid Google OAuth client JSON file.") from error
    if not isinstance(secret, dict) or not secret.get("web"):
        raise ValueError(
            "Use a Google OAuth Web application client and authorize the Clipship callback URL."
        )
    details = secret["web"]
    if not isinstance(details, dict) or not details.get("client_id") or not details.get(
        "client_secret"
    ):
        raise ValueError("The OAuth client file is missing its client ID or secret.")
    CLIENT_SECRET_PATH.write_text(json.dumps(secret), encoding="utf-8")
    try:
        os.chmod(CLIENT_SECRET_PATH, 0o600)
    except OSError:
        logger.warning("Could not restrict OAuth client-secret file permissions on this system.")


def create_oauth_flow(state: str | None = None):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as error:
        raise RuntimeError(
            "Google OAuth libraries are missing. Run run.bat to restore app dependencies."
        ) from error
    if not CLIENT_SECRET_PATH.is_file():
        raise FileNotFoundError(
            "Upload your Google OAuth client JSON in the automation dashboard before connecting."
        )
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = CALLBACK_URL
    return flow


def oauth_authorization_url() -> str:
    state = secrets.token_urlsafe(32)
    flow = create_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    verifier = flow.code_verifier
    if not verifier:
        raise RuntimeError("Could not initialize secure OAuth PKCE verification.")
    set_settings({"oauth_state": state, "oauth_code_verifier": verifier})
    return auth_url


def finish_oauth_callback(code: str, state: str) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key = 'oauth_state'").fetchone()
        verifier_row = db.execute(
            "SELECT value FROM settings WHERE key = 'oauth_code_verifier'"
        ).fetchone()
        expected = row["value"] if row else ""
        if not expected or not hmac.compare_digest(expected, state):
            raise ValueError("Google OAuth state verification failed. Start the connection again.")
        verifier = verifier_row["value"] if verifier_row else ""
        if not verifier:
            raise ValueError("OAuth PKCE verification has expired. Start the connection again.")
        db.execute(
            "DELETE FROM settings WHERE key IN ('oauth_state', 'oauth_code_verifier')"
        )
        db.commit()
    flow = create_oauth_flow(state=state)
    flow.code_verifier = verifier
    try:
        flow.fetch_token(code=code)
    except OAuth2Error as error:
        raise ValueError(
            "Google rejected the authorization code. Start a new connection and approve access once."
        ) from error
    except RequestException as error:
        raise RuntimeError("Could not reach Google to complete OAuth. Try connecting again.") from error
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError(
            "Google did not return a refresh token. Disconnect the app in Google Account "
            "permissions and connect again."
        )
    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        logger.warning("Could not restrict OAuth token-file permissions on this system.")
    set_settings({"oauth_state": ""})


def get_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as error:
        raise RuntimeError(
            "Google OAuth libraries are missing. Run run.bat to restore app dependencies."
        ) from error
    if not TOKEN_PATH.is_file():
        raise PermissionError("Connect a YouTube account in the automation dashboard first.")
    credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            logger.warning("Could not restrict OAuth token-file permissions on this system.")
    if not credentials.valid:
        raise PermissionError("Your YouTube connection expired. Connect your account again.")
    return credentials


def youtube_service(*, timeout: float | None = None):
    try:
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Google's YouTube API client is missing. Run run.bat to restore app dependencies."
        ) from error
    if timeout is not None:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp

        http = AuthorizedHttp(
            get_credentials(),
            http=httplib2.Http(timeout=timeout),
        )
        return build("youtube", "v3", http=http, cache_discovery=False)
    return build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)


def execute(
    request,
    operation: str,
    priority: str,
    task_id: str | None = None,
    task_payload: dict | None = None,
):
    expected_cost = PRIORITY_COSTS.get(operation)
    if expected_cost is None:
        raise ValueError(f"Unsupported YouTube API operation: {operation}")
    if task_payload is not None:
        if not task_id:
            task_id = uuid.uuid4().hex
        queue_api_task(task_id, operation, priority, task_payload)
    reservation = budget.check(expected_cost, priority, operation, task_id=task_id)
    try:
        result = request.execute()
    except Exception as error:
        budget.finish(reservation, "failed")
        if task_payload is not None:
            finish_api_task(task_id, "failed", str(error))
        raise
    budget.finish(reservation, "succeeded")
    if task_payload is not None:
        finish_api_task(task_id, "completed")
    return result


def credentials_status() -> dict:
    return {
        "oauth_client_uploaded": CLIENT_SECRET_PATH.is_file(),
        "youtube_connected": TOKEN_PATH.is_file(),
    }


def disconnect_youtube() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def now_iso() -> str:
    return local_now().isoformat()
