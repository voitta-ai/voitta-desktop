"""OAuth2 provider implementations — Microsoft and Google."""

import base64
import hashlib
import secrets
import webbrowser

import msal
import requests

from .callback import REDIRECT_URI, wait_for_callback

# ── OAuth scope mappings ─────────────────────────────────────────────────────

OAUTH_SCOPES = {
    "microsoft": {
        "rag": ["User.Read"],
    },
    "google": {
        "rag": "openid email profile",
        "google_workspace": (
            "openid email profile"
            " https://www.googleapis.com/auth/spreadsheets"
            " https://www.googleapis.com/auth/documents"
            " https://www.googleapis.com/auth/presentations"
            " https://www.googleapis.com/auth/drive"
        ),
    },
}


def scopes_for_app(app: dict, backend: str):
    """Compute OAuth scopes for one specific backend."""
    app_type = app["type"]
    if app_type == "microsoft":
        return list(OAUTH_SCOPES["microsoft"].get(backend, ["User.Read"]))
    else:
        return OAUTH_SCOPES["google"].get(backend, "openid email profile")


def pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def build_msal_app(app: dict):
    """Build an MSAL PublicClientApplication for a Microsoft app config."""
    tenant_id = app.get("tenant_id", "")
    client_id = app.get("client_id", "")
    if tenant_id and client_id:
        return msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
    return None


# ── Microsoft ────────────────────────────────────────────────────────────────

def do_auth_microsoft(msal_app, app: dict, backend: str) -> dict | None:
    """Run Microsoft OAuth2 flow. Returns token result dict or None."""
    if not msal_app:
        return None

    scopes = scopes_for_app(app, backend)
    auth_url = msal_app.get_authorization_request_url(scopes, redirect_uri=REDIRECT_URI)
    webbrowser.open(auth_url)
    code, error = wait_for_callback()

    if not code:
        return {"error": error or "No authorization code received."}

    result = msal_app.acquire_token_by_authorization_code(
        code, scopes=scopes, redirect_uri=REDIRECT_URI
    )
    return result


def fetch_profile_microsoft(token: str) -> dict | None:
    """Fetch Microsoft user profile (email, name)."""
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            return {
                "email": data.get("mail") or data.get("userPrincipalName", ""),
                "name": data.get("displayName", ""),
            }
    except Exception:
        pass
    return None


def do_refresh_microsoft(msal_app, app: dict, backend: str) -> dict | None:
    """Silently refresh a Microsoft token. Returns new result or None."""
    if not msal_app:
        return None
    accounts = msal_app.get_accounts()
    if not accounts:
        return None
    scopes = scopes_for_app(app, backend)
    return msal_app.acquire_token_silent(scopes, account=accounts[0], force_refresh=True)


# ── Google ───────────────────────────────────────────────────────────────────

def do_auth_google(app: dict, backend: str) -> dict | None:
    """Run Google OAuth2 + PKCE flow. Returns token data dict or None."""
    from urllib.parse import urlencode

    client_id = app.get("client_id", "")
    client_secret = app.get("client_secret", "")
    if not client_id or not client_secret:
        return {"error": "Configure Client ID and Client Secret in Settings first."}

    scopes = scopes_for_app(app, backend)
    verifier, challenge = pkce_pair()
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": scopes,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    webbrowser.open(auth_url)
    code, error = wait_for_callback()

    if not code:
        return {"error": error or "No authorization code received."}

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }, timeout=10)

    if not resp.ok:
        return {"error": f"Token exchange failed: {resp.text[:200]}"}

    return resp.json()


def fetch_profile_google(token: str) -> dict | None:
    """Fetch Google user profile (email, name)."""
    try:
        resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            return {
                "email": data.get("email", ""),
                "name": data.get("name", ""),
            }
    except Exception:
        pass
    return None


def do_refresh_google(app: dict, refresh_token: str) -> dict | None:
    """Silently refresh a Google token. Returns new token data or None."""
    client_id = app.get("client_id", "")
    client_secret = app.get("client_secret", "")
    try:
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }, timeout=10)
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return None
