"""Account store: one JSON file, mode 0600.

Account ids are ``<provider>:<identity>``. Adding an account whose id already
exists replaces its credentials (the "same account re-added after it timed
out, keep the latest" rule) while keeping the user's model mapping, options
and the active selection.
"""

import json
import os
import time
from pathlib import Path

from . import providers

# The global default can be "As is": Claude Code's own login, passed through
# untouched. It behaves like an account everywhere a pick or default is read.
AS_IS = "as-is"
AS_IS_ACCOUNT = {
    "id": AS_IS, "provider": "as_is", "kind": "as_is",
    "label": "As is", "base_url": "https://api.anthropic.com",
    "models": None, "options": {}, "credentials": {}, "info": {},
    "status": "ok", "status_detail": "",
}


def _write_private(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class AccountStore:
    def __init__(self, path: Path):
        self.path = path
        self._data = {"active": AS_IS, "accounts": {}}
        if path.exists():
            self._data = json.loads(path.read_text())
        # Called after every change (from whichever thread made it), e.g. to
        # refresh the menu bar's account list.
        self.listeners: list = []

    def _save(self):
        _write_private(self.path, self._data)
        for listener in self.listeners:
            listener()

    # ---- queries -------------------------------------------------------

    def list(self) -> list[dict]:
        return list(self._data["accounts"].values())

    def get(self, account_id: str) -> dict | None:
        if account_id == AS_IS:
            return AS_IS_ACCOUNT
        return self._data["accounts"].get(account_id)

    @property
    def active_id(self) -> str | None:
        return self._data["active"]

    def active(self) -> dict | None:
        return self.get(self._data["active"]) if self._data["active"] else None

    # ---- mutations -----------------------------------------------------

    def upsert(self, provider: str, identity: str, label: str, credentials: dict,
               *, base_url: str | None = None, models: dict | None = None,
               info: dict | None = None) -> tuple[dict, bool]:
        """Add an account, or replace the credentials of an existing one.

        Returns ``(account, replaced)``.
        """
        p = providers.preset(provider)
        account_id = f"{provider}:{identity}"
        now = time.time()
        existing = self.get(account_id)
        if existing:
            existing.update(label=label, credentials=credentials, status="ok",
                            status_detail="", updated_at=now)
            if info:
                existing["info"] = info
            account, replaced = existing, True
        else:
            account = {
                "id": account_id,
                "provider": provider,
                "kind": p["kind"],
                "label": label,
                "base_url": base_url or p["base_url"],
                "models": models if models is not None else (dict(p["models"]) if p["models"] else None),
                "options": {},
                "credentials": credentials,
                "info": info or {},
                "status": "ok",
                "status_detail": "",
                "created_at": now,
                "updated_at": now,
            }
            self._data["accounts"][account_id] = account
            replaced = False
        if not self._data["active"]:
            self._data["active"] = account_id
        self._save()
        return account, replaced

    def update_credentials(self, account_id: str, credentials: dict):
        account = self._data["accounts"][account_id]
        account["credentials"] = credentials
        account.update(status="ok", status_detail="", updated_at=time.time())
        self._save()

    def set_status(self, account_id: str, status: str, detail: str = ""):
        account = self._data["accounts"].get(account_id)
        if account and (account["status"], account["status_detail"]) != (status, detail):
            account.update(status=status, status_detail=detail)
            self._save()

    def update_settings(self, account_id: str, *, label=None, base_url=None,
                        models=None, options=None):
        account = self._data["accounts"][account_id]
        if label is not None:
            account["label"] = label
        if base_url is not None:
            account["base_url"] = base_url
        if models is not None:
            account["models"] = models
        if options is not None:
            account["options"] = options
        account["updated_at"] = time.time()
        self._save()

    def set_catalog(self, account_id: str, catalog: dict):
        account = self._data["accounts"].get(account_id)
        if account:
            account["catalog"] = catalog
            self._save()

    def fill_models(self, account_id: str, suggested: dict):
        """Set Main/Background where they are still empty (a new account).
        A model the user picked is never replaced."""
        account = self._data["accounts"].get(account_id)
        models = account and account.get("models")
        if models is None:
            return
        changed = False
        for tier in ("big", "small"):
            if not models.get(tier):
                models[tier] = suggested.get(tier) or (suggested.get("big") if tier == "small" else "")
                changed |= bool(models[tier])
        if changed:
            self._save()

    def set_active(self, account_id: str):
        if account_id != AS_IS and account_id not in self._data["accounts"]:
            raise KeyError(account_id)
        self._data["active"] = account_id
        self._save()

    def delete(self, account_id: str):
        self._data["accounts"].pop(account_id, None)
        if self._data["active"] == account_id:
            # Never promote some other account to default behind the user's back.
            self._data["active"] = AS_IS
        self._save()


class SessionPicks:
    """Which account each Claude Code window picked with /llm, by session id.

    Windows without a pick use the global default. A pick survives gateway
    restarts; one unused for PICK_TTL_S is forgotten.
    """

    PICK_TTL_S = 30 * 86400

    def __init__(self, path: Path):
        self.path = path
        self._picks: dict[str, dict] = {}
        if path.exists():
            cutoff = time.time() - self.PICK_TTL_S
            self._picks = {sid: p for sid, p in json.loads(path.read_text()).items()
                           if p.get("used_at", p["picked_at"]) > cutoff}

    def get(self, session_id: str) -> str | None:
        pick = self._picks.get(session_id)
        return pick["account"] if pick else None

    def set(self, session_id: str, account_id: str | None):
        if account_id is None:
            self._picks.pop(session_id, None)
        else:
            now = time.time()
            self._picks[session_id] = {"account": account_id, "picked_at": now, "used_at": now}
        _write_private(self.path, self._picks)

    def touch(self, session_id: str):
        # In memory only; written with the next pick. Losing it costs at most an early expiry.
        if session_id in self._picks:
            self._picks[session_id]["used_at"] = time.time()

    def all(self) -> dict[str, dict]:
        return dict(self._picks)


def public_view(account: dict) -> dict:
    """The account without secrets, for the UI."""
    view = {k: v for k, v in account.items() if k != "credentials"}
    creds = account.get("credentials", {})
    if "api_key" in creds:
        key = creds["api_key"]
        view["key_hint"] = f"…{key[-4:]}" if len(key) > 8 else "…"
    if "expires_at" in creds:
        view["expires_at"] = creds["expires_at"]
    return view
