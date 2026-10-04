"""The LLM accounts page and its API, served on the LLM proxy's own port.

  /_voitta/llm/                 the page (also embedded in Settings → LLMs)
  /_voitta/llm/api/*            its JSON API, and the /llm mod's
  /_voitta/llm/login/*          start a browser login
  /callback                     Claude login redirect (the path Claude's OAuth client allows)
  :1455/auth/callback           ChatGPT login redirect (listener runs only during a login)

``LlmAccounts`` also owns the router and its HTTP session; the proxy calls
``start``/``stop`` around its own lifecycle and ``router.route`` per request.
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import shutil
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web

from . import config, conversations, oauth, providers
from .router import _SAFE_ID, Router, RoutingJournal
from .store import AS_IS, AccountStore, SessionPicks, public_view

log = logging.getLogger("voitta-desktop.llm")

PREFIX = "/_voitta/llm"
STATIC = Path(__file__).parent / "static"
# The /llm mod ships beside this package as a local plugin marketplace.
MOD_DIR = Path(__file__).parent / "mod"
MOD_MARKETPLACE, MOD_NAME = "voitta", "voitta-llm"
# A GUI app's PATH has none of the places `claude` is installed to.
_CLAUDE_DIRS = ("~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin")


def _same_origin(handler):
    """A browser page elsewhere must not drive this API; Claude Code and the
    mod send no Origin, the page itself sends its own."""
    @functools.wraps(handler)
    async def wrapped(self, request):
        origin = request.headers.get("Origin")
        if origin and origin not in self.self_origins:
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        return await handler(self, request)
    return wrapped


class LlmAccounts:
    def __init__(self, port: int, data_dir: Path = config.DATA_DIR, *,
                 conversations_dir: Path | None = None, claude_projects: list[Path] | None = None):
        self.port = port
        self.ui_url = f"http://localhost:{port}{PREFIX}/"
        self.self_origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
        self.store = AccountStore(data_dir / "accounts.json")
        self.picks = SessionPicks(data_dir / "sessions.json")
        self.pending = oauth.PendingLogins(data_dir / "invites.json")
        self.journal = RoutingJournal(data_dir / "routes")
        # Beside llm/, not inside it: ~/.voitta-desktop/conversations.
        self.conversations_dir = conversations_dir or data_dir.parent / "conversations"
        self.claude_projects = claude_projects
        self.device_invites: dict[str, dict] = {}
        self.http: aiohttp.ClientSession | None = None
        self.router: Router | None = None
        self._openai_runner: web.AppRunner | None = None
        self._openai_timer: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._refresher: asyncio.Task | None = None

    # ---- lifecycle (called by the proxy) -------------------------------------

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)
        self.http = aiohttp.ClientSession(timeout=timeout)
        self.router = Router(self.store, self.picks, self.http, ui_url=self.ui_url, self_origins=self.self_origins,
                             journal=self.journal)
        await asyncio.to_thread(self.journal.prune)
        self._refresher = asyncio.create_task(self._refresh_loop(), name="llm-token-refresh")

    async def stop(self):
        if self._refresher:
            self._refresher.cancel()
        await self._stop_openai_listener()
        for d in self.device_invites.values():
            if d.get("task"):
                d["task"].cancel()
        for task in list(self._background):
            task.cancel()
        if self.http:
            await self.http.close()

    def add_routes(self, router: web.UrlDispatcher):
        r, p = router, PREFIX
        r.add_get(p, self.index_redirect)
        r.add_get(p + "/", self.index)
        r.add_get("/callback", self.claude_callback)
        r.add_get(p + "/login/claude", self.claude_login)
        r.add_get(p + "/login/openai", self.openai_login)
        r.add_get(p + "/api/state", self.state)
        r.add_post(p + "/api/invites", self.create_invite)
        r.add_delete(p + "/api/invites/{state}", self.cancel_invite)
        r.add_post(p + "/api/device-invites", self.create_device_invite)
        r.add_delete(p + "/api/device-invites/{id}", self.cancel_device_invite)
        r.add_post(p + "/api/login/claude/manual", self.claude_manual_finish)
        r.add_post(p + "/api/accounts", self.add_api_key_account)
        r.add_post(p + "/api/accounts/{id}/activate", self.activate)
        r.add_get(p + "/api/llm/options", self.llm_options)
        r.add_put(p + "/api/llm/sessions/{session}", self.llm_pick)
        r.add_post(p + "/api/conversations", self.store_conversation)
        r.add_get(p + "/api/mod", self.mod_status)
        r.add_post(p + "/api/mod/install", self.mod_install)
        r.add_post(p + "/api/accounts/{id}/refresh", self.refresh)
        r.add_post(p + "/api/accounts/{id}/test", self.test_account)
        r.add_post(p + "/api/accounts/{id}/models", self.refresh_models)
        r.add_patch(p + "/api/accounts/{id}", self.update_account)
        r.add_delete(p + "/api/accounts/{id}", self.delete_account)

    def _fetch_catalog_soon(self, account_id: str):
        account = self.store.get(account_id)
        if account:
            task = asyncio.create_task(self.router.refresh_catalog(account))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _refresh_loop(self):
        for account in self.store.list():
            if not account.get("catalog") and account["status"] != "needs_login":
                self._fetch_catalog_soon(account["id"])
        while True:
            try:
                await self.router.refresh_expiring()
            except Exception:
                log.exception("token refresh sweep failed")
            await asyncio.sleep(config.REFRESH_SWEEP_EVERY_S)

    # ---- the page ----------------------------------------------------------------

    async def index_redirect(self, request):
        raise web.HTTPFound(PREFIX + "/")

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @_same_origin
    async def state(self, request):
        return web.json_response({
            "port": self.port,
            "active": self.store.active_id,
            "accounts": [public_view(a) for a in self.store.list()],
            "windows": self._windows_view(),
            "presets": {k: {"label": p["label"], "auth": p["auth"], "base_url": p["base_url"],
                            "models": p["models"]} for k, p in providers.PRESETS.items()},
            "requests": list(self.router.requests)[:50],
            "invites": self.pending.invites("claude"),
            "device_invites": self._device_view(),
            "now": time.time(),
        })

    def _added(self, result: dict, provider: str):
        account, replaced = self.store.upsert(provider, result["identity"], result["label"],
                                              result["credentials"], info=result["info"])
        log.info("%s account %s", "replaced" if replaced else "added", account["id"])
        self._fetch_catalog_soon(account["id"])
        q = urllib.parse.urlencode({"added": account["label"], "replaced": int(replaced)})
        raise web.HTTPFound(f"{self.ui_url}?{q}")

    def _failed(self, message: str):
        raise web.HTTPFound(f"{self.ui_url}?{urllib.parse.urlencode({'error': message})}")

    # ---- Claude login --------------------------------------------------------

    async def claude_login(self, request):
        url = oauth.claude_authorize_url(self.pending, f"http://localhost:{self.port}/callback")
        raise web.HTTPFound(url)

    async def claude_callback(self, request):
        q = request.query
        if "error" in q:
            self._failed(f"Claude login: {q.get('error_description') or q['error']}")
        try:
            login = self.pending.pop(q.get("state", ""))
            result = await oauth.claude_exchange(self.http, q["code"], login, q["state"])
        except (oauth.OAuthError, KeyError, aiohttp.ClientError) as e:
            self._failed(f"Claude login failed: {e}")
        self._added(result, "claude")

    @_same_origin
    async def create_invite(self, request):
        """An authorize link for someone else (or a browser on another machine).

        Anthropic's own redirect page shows them a ``code#state`` to send back;
        no callback to this machine is needed.
        """
        note = ((await request.json()).get("note") or "").strip()[:80]
        url = oauth.claude_authorize_url(self.pending, oauth.CLAUDE["manual_redirect_uri"], invite_note=note)
        return web.json_response({"url": url, "invites": self.pending.invites("claude")})

    @_same_origin
    async def cancel_invite(self, request):
        self.pending.discard(request.match_info["state"])
        return web.json_response({"invites": self.pending.invites("claude")})

    @_same_origin
    async def claude_manual_finish(self, request):
        pasted = "".join((await request.json()).get("code", "").split())
        code, _, state = pasted.partition("#")
        if not state:
            # Just the code: fine as long as only one invite is waiting.
            invites = self.pending.invites("claude")
            if len(invites) != 1:
                return web.json_response({"error": "Paste the whole code including the part after '#' "
                                                   "(it says which invite it answers)."}, status=400)
            state = invites[0]["state"]
        try:
            login = self.pending.get(state)
            result = await oauth.claude_exchange(self.http, code, login, state)
        except (oauth.OAuthError, aiohttp.ClientError) as e:
            # The invite stays open: a mistyped or stale code can be retried with a fresh one.
            return web.json_response({"error": f"{e} The link is still valid; they can open it "
                                               f"again for a new code."}, status=400)
        self.pending.discard(state)
        if login.get("note"):
            result["info"]["invited_as"] = login["note"]
        account, replaced = self.store.upsert("claude", result["identity"], result["label"],
                                              result["credentials"], info=result["info"])
        log.info("%s account %s via invite", "replaced" if replaced else "added", account["id"])
        self._fetch_catalog_soon(account["id"])
        return web.json_response({"account": public_view(account), "replaced": replaced})

    # ---- ChatGPT device-code invite --------------------------------------------
    # OpenAI's device codes live ~15 minutes, so these stay in memory; a
    # restart cancels them and a new code is needed.

    def _device_view(self) -> list[dict]:
        return [{k: v for k, v in d.items() if k not in ("task", "device_auth_id")}
                for d in self.device_invites.values()]

    @_same_origin
    async def create_device_invite(self, request):
        note = ((await request.json()).get("note") or "").strip()[:80]
        try:
            device = await oauth.openai_device_start(self.http)
        except (oauth.OAuthError, aiohttp.ClientError) as e:
            return web.json_response({"error": str(e)}, status=502)
        invite_id = device["device_auth_id"][-12:]
        entry = {"id": invite_id, "note": note, "user_code": device["user_code"],
                 "url": oauth.OPENAI["device_page"], "expires": device["expires_at"],
                 "status": "waiting", "detail": "", "device_auth_id": device["device_auth_id"]}
        self.device_invites[invite_id] = entry
        entry["task"] = asyncio.create_task(self._poll_device(entry, device))
        return web.json_response({"invite": self._device_view()[-1]})

    async def _poll_device(self, entry: dict, device: dict):
        try:
            while True:
                await asyncio.sleep(device["interval"])
                if time.time() > device["expires_at"]:
                    entry.update(status="expired", detail="The code expired before it was approved.")
                    return
                try:
                    result = await oauth.openai_device_poll(self.http, device)
                except aiohttp.ClientError as e:
                    log.warning("device poll %s: %s", entry["id"], e)  # network blip: the next poll retries
                    continue
                if result is None:
                    continue
                if entry["note"]:
                    result["info"]["invited_as"] = entry["note"]
                account, replaced = self.store.upsert("openai", result["identity"], result["label"],
                                                      result["credentials"], info=result["info"])
                log.info("%s account %s via device code", "replaced" if replaced else "added", account["id"])
                self._fetch_catalog_soon(account["id"])
                entry.update(status="added", detail=("Re-logged in " if replaced else "Added ") + account["label"])
                return
        except oauth.OAuthError as e:
            entry.update(status="failed", detail=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("device login %s", entry["id"])
            entry.update(status="failed", detail=f"unexpected error: {e}")
        finally:
            entry["task"] = None

    @_same_origin
    async def cancel_device_invite(self, request):
        entry = self.device_invites.pop(request.match_info["id"], None)
        if entry and entry.get("task"):
            entry["task"].cancel()
        return web.json_response({"device_invites": self._device_view()})

    # ---- ChatGPT login -------------------------------------------------------

    async def openai_login(self, request):
        try:
            await self._start_openai_listener()
        except OSError:
            self._failed(f"Port {oauth.OPENAI['callback_port']} is busy (a Codex login in progress?). "
                         "Finish or cancel it and try again.")
        raise web.HTTPFound(oauth.openai_authorize_url(self.pending))

    async def _start_openai_listener(self):
        if self._openai_runner is None:
            app = web.Application()
            app.router.add_get("/auth/callback", self.openai_callback)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", oauth.OPENAI["callback_port"]).start()
            except OSError:
                await runner.cleanup()
                raise
            self._openai_runner = runner
            log.info("listening on :%s for the ChatGPT login", oauth.OPENAI["callback_port"])
        if self._openai_timer:
            self._openai_timer.cancel()
        self._openai_timer = asyncio.create_task(self._stop_openai_listener_later())

    async def _stop_openai_listener_later(self):
        await asyncio.sleep(oauth.PENDING_TTL_S)
        self._openai_timer = None
        await self._stop_openai_listener()

    async def _stop_openai_listener(self):
        if self._openai_timer:
            self._openai_timer.cancel()
            self._openai_timer = None
        if self._openai_runner:
            runner, self._openai_runner = self._openai_runner, None
            await runner.cleanup()

    async def openai_callback(self, request):
        q = request.query
        try:
            if "error" in q:
                self._failed(f"ChatGPT login: {q.get('error_description') or q['error']}")
            try:
                login = self.pending.pop(q.get("state", ""))
                result = await oauth.openai_exchange(self.http, q["code"], login)
            except (oauth.OAuthError, KeyError, aiohttp.ClientError) as e:
                self._failed(f"ChatGPT login failed: {e}")
            self._added(result, "openai")
        finally:
            if not self.pending.any_for("openai"):
                # Shut the listener down after this response has gone out.
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._stop_openai_listener()))

    # ---- account management --------------------------------------------------

    def _account(self, request, *, allow_as_is=False) -> dict:
        account_id = request.match_info["id"]
        if account_id == AS_IS and not allow_as_is:
            raise web.HTTPBadRequest(text="'As is' is Claude Code's own login; there is nothing to change here")
        account = self.store.get(account_id)
        if not account:
            raise web.HTTPNotFound(text="no such account")
        return account

    # ---- /llm: per-window picks (the voitta-llm mod calls these) -------------

    def _llm_option(self, account: dict) -> dict:
        models = account.get("models") or {}
        if account["kind"] == "as_is":
            provider_label = "Claude Code's own login"
        else:
            provider_label = providers.PRESETS.get(account["provider"], {}).get("label", account["provider"])
        return {"id": account["id"], "label": account["label"], "provider": account["provider"],
                "provider_label": provider_label, "status": account["status"],
                "status_detail": account.get("status_detail", ""),
                "main": models.get("big") or None, "background": models.get("small") or None}

    def options(self) -> list[dict]:
        """Every choice, "As is" first: for the mod and the menu bar."""
        return [self._llm_option(self.store.get(AS_IS))] + [self._llm_option(a) for a in self.store.list()]

    def _llm_view(self, session_id: str) -> dict:
        default = self.store.get(self.store.active_id) if self.store.active_id else None
        return {
            "session": session_id,
            "pick": self.picks.get(session_id) if session_id else None,
            "default": self._llm_option(default) if default else None,
            "options": self.options(),
            "ui": "Voitta Desktop → Settings → LLMs",
        }

    @_same_origin
    async def llm_options(self, request):
        return web.json_response(self._llm_view(request.query.get("session", "")))

    @_same_origin
    async def llm_pick(self, request):
        session_id = request.match_info["session"]
        account_id = (await request.json()).get("account")
        if account_id in (None, "", "default"):
            self.picks.set(session_id, None)
        elif not self.store.get(account_id):
            return web.json_response({"error": f"no account {account_id!r}"}, status=400)
        else:
            self.picks.set(session_id, account_id)
        log.info("window %s: %s", session_id[:8], account_id or "default")
        return web.json_response(self._llm_view(session_id))

    # ---- /voitta-store: keep this window's conversation --------------------------

    @_same_origin
    async def store_conversation(self, request):
        data = await request.json()
        session_id = str(data.get("session") or "")
        if not _SAFE_ID.fullmatch(session_id):
            return web.json_response({"error": "a Claude Code session id is required"}, status=400)
        projects = self.claude_projects or conversations.claude_projects_dirs()
        transcript = conversations.find_transcript(session_id, data.get("transcript_path"), projects)
        if not transcript:
            return web.json_response({"error": f"Claude Code's transcript for session {session_id[:8]} "
                                               "was not found"}, status=404)
        pick = self.picks.get(session_id)
        effective = self.store.get(pick or self.store.active_id or "") or {}
        llm = {"window_pick": pick, "default": self.store.active_id,
               "account": {k: effective.get(k) for k in ("id", "label", "provider", "kind", "models", "options")},
               "route": "window" if pick else "default"}
        claude_code = {k: data[k] for k in ("model", "cwd", "entrypoint") if data.get(k)}
        routing = self.journal.read(session_id)

        meta = await asyncio.to_thread(conversations.store, self.conversations_dir, session_id, transcript,
                                       routing, claude_code=claude_code, llm=llm)
        log.info("stored conversation %s: %s", session_id[:8], meta["counts"])
        return web.json_response({"path": str(self.conversations_dir / session_id), "bytes": meta["bytes"],
                                  **meta["counts"]})

    # ---- the /llm mod: one-click install into Claude Code ---------------------

    @staticmethod
    def _claude_bin() -> str | None:
        path = os.pathsep.join([os.environ.get("PATH", "")] + [os.path.expanduser(d) for d in _CLAUDE_DIRS])
        return shutil.which("claude", path=path)

    async def _claude(self, *args: str) -> tuple[int, str]:
        claude = self._claude_bin()
        if not claude:
            return 127, "Claude Code (the `claude` command) was not found in PATH or " + ", ".join(_CLAUDE_DIRS)
        proc = await asyncio.create_subprocess_exec(
            claude, "plugin", *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"claude plugin {' '.join(args)} timed out"
        return proc.returncode, out.decode(errors="replace").strip()

    async def _mod_view(self) -> dict:
        available = json.loads((MOD_DIR / MOD_NAME / ".claude-plugin" / "plugin.json").read_text())["version"]
        code, out = await self._claude("list", "--json")
        if code != 0:
            return {"error": out[-500:], "available": available, "installed": None}
        mine = next((p for p in json.loads(out) if p["id"] == f"{MOD_NAME}@{MOD_MARKETPLACE}"), None)
        return {"available": available, "installed": mine and mine["version"],
                "enabled": bool(mine and mine["enabled"]), "error": None}

    @_same_origin
    async def mod_status(self, request):
        return web.json_response(await self._mod_view())

    @_same_origin
    async def mod_install(self, request):
        """Register the bundled marketplace (re-registering it if it points
        somewhere else, e.g. an older copy), then install or update the mod."""
        steps = []

        async def run(*args):
            code, out = await self._claude(*args)
            steps.append({"command": "claude plugin " + " ".join(args), "exit": code, "output": out[-800:]})
            return code, out

        code, out = await self._claude("marketplace", "list", "--json")
        registered = next((m for m in json.loads(out) if m["name"] == MOD_MARKETPLACE), None) if code == 0 else None
        here = str(MOD_DIR)
        plan = []
        if registered and registered.get("path") != here:
            plan.append(("marketplace", "remove", MOD_MARKETPLACE))
            registered = None
        plan.append(("marketplace", "update", MOD_MARKETPLACE) if registered else ("marketplace", "add", here))
        plan.append(("install", f"{MOD_NAME}@{MOD_MARKETPLACE}"))
        for args in plan:
            code, _ = await run(*args)
            if code != 0:
                return web.json_response({"ok": False, "steps": steps, **await self._mod_view()}, status=500)
        view = await self._mod_view()
        if view["installed"] != view["available"]:
            await run("update", f"{MOD_NAME}@{MOD_MARKETPLACE}")
            view = await self._mod_view()
        log.info("mod install: %s", [(s["command"], s["exit"]) for s in steps])
        return web.json_response({"ok": view["installed"] == view["available"], "steps": steps, **view})

    def _windows_view(self) -> list[dict]:
        picks = self.picks.all()
        seen = self.router.sessions
        rows = []
        for sid in set(picks) | set(seen):
            s, pick = seen.get(sid, {}), picks.get(sid)
            if not s and time.time() - pick["picked_at"] > 86400:
                continue  # an old pick from a window that hasn't spoken since Voitta Desktop started
            picked = self.store.get(pick["account"]) if pick else None
            routed = self.store.get(s["account"]) if s.get("account") else None
            rows.append({"session": sid, "pick": pick["account"] if pick else None,
                         "pick_label": picked["label"] if picked else (pick["account"] if pick else None),
                         "routed_label": routed["label"] if routed else None, "route": s.get("route"),
                         "requests": s.get("requests", 0),
                         "last_seen": s.get("last_seen") or (pick or {}).get("used_at")})
        rows.sort(key=lambda r: -(r["last_seen"] or 0))
        return rows

    @_same_origin
    async def add_api_key_account(self, request):
        data = await request.json()
        provider = data.get("provider", "")
        try:
            p = providers.preset(provider)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        key = (data.get("api_key") or "").strip()
        base_url = (data.get("base_url") or p["base_url"]).strip()
        if p["auth"] != "api_key" or not key or not base_url:
            return web.json_response({"error": "provider, api_key and base_url are required"}, status=400)
        fingerprint = hashlib.sha256(f"{base_url}|{key}".encode()).hexdigest()[:12]
        models = p["models"]
        account, replaced = self.store.upsert(
            provider, fingerprint, data.get("label") or f"{p['label'].split(' (')[0]} …{key[-4:]}",
            {"api_key": key}, base_url=base_url, models=dict(models) if models else None)
        self._fetch_catalog_soon(account["id"])
        return web.json_response({"account": public_view(account), "replaced": replaced})

    @_same_origin
    async def activate(self, request):
        self.store.set_active(self._account(request, allow_as_is=True)["id"])
        return web.json_response({"active": self.store.active_id})

    @_same_origin
    async def update_account(self, request):
        account = self._account(request)
        data = await request.json()
        options = {**account.get("options", {}), **data["options"]} if "options" in data else None
        self.store.update_settings(account["id"], label=data.get("label"), base_url=data.get("base_url"),
                                   models=data.get("models"), options=options)
        return web.json_response({"account": public_view(self.store.get(account["id"]))})

    @_same_origin
    async def delete_account(self, request):
        self.store.delete(self._account(request)["id"])
        return web.json_response({"active": self.store.active_id})

    @_same_origin
    async def refresh(self, request):
        account = self._account(request)
        if "refresh_token" not in account["credentials"]:
            return web.json_response({"error": "API-key accounts do not refresh"}, status=400)
        try:
            await self.router.credentials(account, force_refresh=True)
        except Exception as e:  # GatewayError or network trouble; show it in the UI
            return web.json_response({"error": getattr(e, "message", str(e))}, status=400)
        return web.json_response({"account": public_view(self.store.get(account["id"]))})

    @_same_origin
    async def refresh_models(self, request):
        catalog = await self.router.refresh_catalog(self._account(request))
        if catalog["error"]:
            return web.json_response({"error": catalog["error"]}, status=502)
        return web.json_response({"count": len(catalog["models"])})

    @_same_origin
    async def test_account(self, request):
        """A tiny Claude-Code-shaped request straight to this account (not through
        the proxy's middleware, so it never shows up as a conversation)."""
        account = self._account(request)
        body = {"model": "claude-haiku-4-5", "max_tokens": 64, "stream": False,
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
        if account["kind"] == "anthropic_oauth":
            body["system"] = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
        entry = self.router._entry("/v1/messages", None)
        entry.update(account=account["label"], provider=account["provider"], route="test")
        started = time.monotonic()
        routed = await self.router.answer(account, "POST", "/v1/messages",
                                          {"anthropic-version": "2023-06-01", "content-type": "application/json"},
                                          json.dumps(body).encode(), entry)
        try:
            raw = await routed.read()
        finally:
            await routed.aclose()
        ms = int((time.monotonic() - started) * 1000)
        try:
            result = json.loads(raw)
        except ValueError:
            result = {"error": {"message": raw[:500].decode("utf-8", "replace")}}
        if routed.status != 200:
            return web.json_response({"ok": False, "status": routed.status, "ms": ms,
                                      "error": (result.get("error") or {}).get("message", result)})
        text = "".join(b.get("text", "") for b in result.get("content", []) if b["type"] == "text")
        return web.json_response({"ok": True, "ms": ms, "reply": text, "usage": result.get("usage")})
