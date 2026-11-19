"""Subscription logins (PKCE OAuth) and token refresh.

The client ids, endpoints and scopes are the ones Claude Code 2.1.x and the
Codex CLI 0.160 use for their own logins, so the provider sees the same
authorization flow either client would start.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import aiohttp

CLAUDE = {
    "authorize_url": "https://claude.com/cai/oauth/authorize",
    "token_url": "https://platform.claude.com/v1/oauth/token",
    "profile_url": "https://api.anthropic.com/api/oauth/profile",
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "manual_redirect_uri": "https://platform.claude.com/oauth/code/callback",
    # Claude Code asks for org:create_api_key at login but leaves it out on refresh.
    "login_scopes": ["org:create_api_key", "user:profile", "user:inference",
                     "user:sessions:claude_code", "user:mcp_servers",
                     "user:file_upload", "user:plugins"],
    "refresh_scopes": ["user:profile", "user:inference", "user:sessions:claude_code",
                       "user:mcp_servers", "user:file_upload", "user:plugins"],
}
OAUTH_BETA = "oauth-2025-04-20"

OPENAI = {
    "authorize_url": "https://auth.openai.com/oauth/authorize",
    "token_url": "https://auth.openai.com/oauth/token",
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    # The Codex client is registered for this exact redirect, port included.
    "callback_port": 1455,
    "redirect_uri": "http://localhost:1455/auth/callback",
    "scope": "openid profile email offline_access api.connectors.read api.connectors.invoke",
    "refresh_scope": "openid profile email",
    # Device-code login (`codex login --device-auth`): for someone on another
    # machine. They open device_page, enter the code; we poll for the result.
    "device_usercode_url": "https://auth.openai.com/api/accounts/deviceauth/usercode",
    "device_token_url": "https://auth.openai.com/api/accounts/deviceauth/token",
    "device_page": "https://auth.openai.com/codex/device",
    "device_redirect_uri": "https://auth.openai.com/deviceauth/callback",
}

PENDING_TTL_S = 30 * 60  # long enough to sign in to the provider first
INVITE_TTL_S = 7 * 24 * 3600


class OAuthError(Exception):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it (it came straight from the issuer over TLS)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class PendingLogins:
    """Logins waiting for their callback or pasted code, keyed by ``state``.

    Browser logins live in memory for PENDING_TTL_S. Invites (an authorize
    link sent to someone else, who sends back the code) wait much longer and
    are saved to disk so a restart doesn't lose them.
    """

    def __init__(self, path: Path | None = None):
        self.path = path
        self._pending: dict[str, dict] = {}
        if path and path.exists():
            self._pending = json.loads(path.read_text())
        self._expire()

    def add(self, provider: str, verifier: str, redirect_uri: str, *, invite_note: str | None = None) -> str:
        self._expire()
        state = _b64url(secrets.token_bytes(24))
        now = time.time()
        invite = invite_note is not None
        self._pending[state] = {"provider": provider, "verifier": verifier, "redirect_uri": redirect_uri,
                                "created": now, "expires": now + (INVITE_TTL_S if invite else PENDING_TTL_S),
                                "invite": invite, "note": invite_note or ""}
        return state

    def set_url(self, state: str, url: str):
        self._pending[state]["url"] = url
        self._save()

    def get(self, state: str) -> dict:
        self._expire()
        try:
            return self._pending[state]
        except KeyError:
            raise OAuthError("Unknown or expired login attempt; start again.") from None

    def pop(self, state: str) -> dict:
        login = self.get(state)
        self.discard(state)
        return login

    def discard(self, state: str):
        if self._pending.pop(state, None) and self.path:
            self._save()

    def any_for(self, provider: str) -> bool:
        self._expire()
        return any(p["provider"] == provider for p in self._pending.values())

    def invites(self, provider: str) -> list[dict]:
        self._expire()
        return sorted(({"state": s, **{k: p.get(k) for k in ("note", "url", "created", "expires")}}
                       for s, p in self._pending.items() if p["invite"] and p["provider"] == provider),
                      key=lambda i: i["created"])

    def _expire(self):
        now = time.time()
        dead = [s for s, p in self._pending.items() if p["expires"] < now]
        for state in dead:
            del self._pending[state]
        if dead:
            self._save()

    def _save(self):
        if not self.path:
            return
        invites = {s: p for s, p in self._pending.items() if p["invite"]}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(invites, f, indent=2)
        os.replace(tmp, self.path)


# ---- Claude -----------------------------------------------------------------

def claude_authorize_url(pending: PendingLogins, redirect_uri: str, *, invite_note: str | None = None) -> str:
    verifier, challenge = _pkce()
    state = pending.add("claude", verifier, redirect_uri, invite_note=invite_note)
    params = {
        "code": "true",
        "client_id": CLAUDE["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(CLAUDE["login_scopes"]),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = f"{CLAUDE['authorize_url']}?{urllib.parse.urlencode(params)}"
    if invite_note is not None:
        pending.set_url(state, url)
    return url


async def claude_exchange(http: aiohttp.ClientSession, code: str, login: dict, state: str) -> dict:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": login["redirect_uri"],
        "client_id": CLAUDE["client_id"],
        "code_verifier": login["verifier"],
        "state": state,
    }
    tok = await _post_json(http, CLAUDE["token_url"], body)
    account = tok.get("account") or {}
    org = tok.get("organization") or {}
    if not account.get("uuid"):
        profile = await claude_profile(http, tok["access_token"])
        account = {"uuid": profile["account"]["uuid"],
                   "email_address": profile["account"].get("email")}
        org = profile.get("organization") or {}
    return {
        "identity": f"{account['uuid']}:{org.get('uuid', '-')}",
        "label": account.get("email_address") or account["uuid"],
        "credentials": _claude_creds(tok),
        "info": {"email": account.get("email_address"), "organization": org.get("name"),
                 "organization_uuid": org.get("uuid")},
    }


async def claude_profile(http: aiohttp.ClientSession, access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "anthropic-beta": OAUTH_BETA}
    async with http.get(CLAUDE["profile_url"], headers=headers) as r:
        if r.status != 200:
            raise OAuthError(f"profile lookup failed ({r.status}): {await r.text()}")
        return await r.json()


async def claude_refresh(http: aiohttp.ClientSession, creds: dict) -> dict:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": CLAUDE["client_id"],
        "scope": " ".join(CLAUDE["refresh_scopes"]),
    }
    tok = await _post_json(http, CLAUDE["token_url"], body)
    tok.setdefault("refresh_token", creds["refresh_token"])
    return _claude_creds(tok)


def _claude_creds(tok: dict) -> dict:
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + float(tok.get("expires_in", 3600)),
    }


# ---- OpenAI (ChatGPT / Codex) -----------------------------------------------

def openai_authorize_url(pending: PendingLogins) -> str:
    verifier, challenge = _pkce()
    state = pending.add("openai", verifier, OPENAI["redirect_uri"])
    params = {
        "response_type": "code",
        "client_id": OPENAI["client_id"],
        "redirect_uri": OPENAI["redirect_uri"],
        "scope": OPENAI["scope"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    }
    return f"{OPENAI['authorize_url']}?{urllib.parse.urlencode(params)}"


async def openai_exchange(http: aiohttp.ClientSession, code: str, login: dict) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": login["redirect_uri"],
        "client_id": OPENAI["client_id"],
        "code_verifier": login["verifier"],
    }
    async with http.post(OPENAI["token_url"], data=form) as r:
        if r.status != 200:
            raise OAuthError(f"token exchange failed ({r.status}): {await r.text()}")
        tok = await r.json()
    creds = _openai_creds(tok)
    claims = jwt_claims(tok["id_token"])
    auth = claims.get("https://api.openai.com/auth", {})
    user = auth.get("chatgpt_user_id") or auth.get("user_id") or claims.get("sub", "?")
    return {
        "identity": f"{user}:{creds['account_id']}",
        "label": " · ".join(x for x in (claims.get("email") or user, auth.get("chatgpt_plan_type")) if x),
        "credentials": creds,
        "info": {"email": claims.get("email"), "plan": auth.get("chatgpt_plan_type")},
    }


async def openai_device_start(http: aiohttp.ClientSession) -> dict:
    """Ask OpenAI for a device code. Returns device_auth_id, user_code, interval (s), expires_at (epoch)."""
    async with http.post(OPENAI["device_usercode_url"], json={"client_id": OPENAI["client_id"]}) as r:
        text = await r.text()
        if r.status != 200:
            raise OAuthError(f"device code request failed ({r.status}): {text[:300]}")
    d = json.loads(text)
    return {"device_auth_id": d["device_auth_id"], "user_code": d["user_code"],
            "interval": max(1, int(d["interval"])),
            "expires_at": datetime.fromisoformat(d["expires_at"]).timestamp()}


async def openai_device_poll(http: aiohttp.ClientSession, device: dict) -> dict | None:
    """None while the user hasn't approved yet; the finished login once they have."""
    body = {"device_auth_id": device["device_auth_id"], "user_code": device["user_code"]}
    async with http.post(OPENAI["device_token_url"], json=body) as r:
        text = await r.text()
        if r.status == 200:
            d = json.loads(text)
            login = {"redirect_uri": OPENAI["device_redirect_uri"], "verifier": d["code_verifier"]}
            return await openai_exchange(http, d["authorization_code"], login)
    try:
        code = json.loads(text)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    if code == "deviceauth_authorization_pending":
        return None
    raise OAuthError(f"device login failed ({r.status}): {text[:300]}")


async def openai_refresh(http: aiohttp.ClientSession, creds: dict) -> dict:
    body = {
        "client_id": OPENAI["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "scope": OPENAI["refresh_scope"],
    }
    tok = await _post_json(http, OPENAI["token_url"], body)
    tok.setdefault("refresh_token", creds["refresh_token"])
    tok.setdefault("id_token", creds.get("id_token", ""))
    new = _openai_creds(tok)
    new["account_id"] = new["account_id"] or creds.get("account_id")
    return new


def _openai_creds(tok: dict) -> dict:
    id_claims = jwt_claims(tok.get("id_token", ""))
    access_claims = jwt_claims(tok["access_token"])
    account_id = (id_claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
                  or access_claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id"))
    if "expires_in" in tok:
        expires_at = time.time() + float(tok["expires_in"])
    else:
        expires_at = float(access_claims.get("exp", time.time() + 3600))
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "id_token": tok.get("id_token", ""),
        "account_id": account_id,
        "expires_at": expires_at,
    }


# ---- shared -------------------------------------------------------------------

class RefreshRejected(OAuthError):
    """The refresh token is no longer accepted; the user has to log in again."""


async def _post_json(http: aiohttp.ClientSession, url: str, body: dict) -> dict:
    async with http.post(url, json=body) as r:
        text = await r.text()
        if r.status in (400, 401) and "invalid_grant" in text:
            raise RefreshRejected(text[:300])
        if r.status != 200:
            raise OAuthError(f"{url} failed ({r.status}): {text[:300]}")
        return json.loads(text)


async def refresh(http: aiohttp.ClientSession, account: dict) -> dict:
    if account["provider"] == "claude":
        return await claude_refresh(http, account["credentials"])
    if account["provider"] == "openai":
        return await openai_refresh(http, account["credentials"])
    raise OAuthError(f"{account['provider']} accounts do not refresh")
