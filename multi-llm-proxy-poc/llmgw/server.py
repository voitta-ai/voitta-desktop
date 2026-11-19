"""HTTP server: the Anthropic-shaped endpoint for Claude Code, plus the account UI.

  /v1/messages, /v1/messages/count_tokens   -> active account
  other /v1/*, /api/*                       -> passed through for Claude accounts
  /                                         -> account UI
  /_gw/api/*                                -> UI JSON API
  /callback                                 -> Claude login redirect
  :1455/auth/callback                       -> ChatGPT login redirect (listener runs only during a login)
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web

from . import config, oauth, providers, sse
from .store import AS_IS, AccountStore, SessionPicks, public_view
from .upstream import ACCOUNT_HEADER, Gateway

log = logging.getLogger("llmgw")
# The /llm mod ships beside the gateway as a local plugin marketplace.
MOD_DIR = Path(__file__).resolve().parent.parent / "mod"
MOD_MARKETPLACE, MOD_NAME = "voitta", "voitta-llm"
STATIC = Path(__file__).parent / "static"


def _self_origin() -> set[str]:
    return {f"http://localhost:{config.PORT}", f"http://127.0.0.1:{config.PORT}"}


@web.middleware
async def same_origin_only(request, handler):
    # Claude Code sends no Origin. A browser does, and a web page must not be
    # able to drive the UI API or spend the user's subscriptions.
    origin = request.headers.get("Origin")
    if origin and origin not in _self_origin():
        return web.json_response({"error": "cross-origin request refused"}, status=403)
    return await handler(request)


class App:
    def __init__(self, store: AccountStore, picks: SessionPicks | None = None):
        self.store = store
        self.picks = picks or SessionPicks(store.path.with_name("sessions.json"))
        self.device_invites: dict[str, dict] = {}
        self.pending = oauth.PendingLogins(config.INVITES_FILE)
        self.http: aiohttp.ClientSession | None = None
        self.gateway: Gateway | None = None
        self._openai_runner: web.AppRunner | None = None
        self._openai_timer: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._refresher: asyncio.Task | None = None

    def build(self) -> web.Application:
        app = web.Application(middlewares=[same_origin_only], client_max_size=64 * 1024 ** 2)
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/callback", self.claude_callback)
        r.add_get("/_gw/login/claude", self.claude_login)
        r.add_get("/_gw/login/openai", self.openai_login)
        r.add_get("/_gw/api/state", self.state)
        r.add_post("/_gw/api/invites", self.create_invite)
        r.add_delete("/_gw/api/invites/{state}", self.cancel_invite)
        r.add_post("/_gw/api/device-invites", self.create_device_invite)
        r.add_delete("/_gw/api/device-invites/{id}", self.cancel_device_invite)
        r.add_post("/_gw/api/login/claude/manual", self.claude_manual_finish)
        r.add_post("/_gw/api/accounts", self.add_api_key_account)
        r.add_post("/_gw/api/accounts/{id}/activate", self.activate)
        r.add_get("/_gw/api/llm/options", self.llm_options)
        r.add_get("/_gw/api/mod", self.mod_status)
        r.add_post("/_gw/api/mod/install", self.mod_install)
        r.add_put("/_gw/api/llm/sessions/{session}", self.llm_pick)
        r.add_post("/_gw/api/accounts/{id}/refresh", self.refresh)
        r.add_post("/_gw/api/accounts/{id}/test", self.test_account)
        r.add_post("/_gw/api/accounts/{id}/models", self.refresh_models)
        r.add_patch("/_gw/api/accounts/{id}", self.update_account)
        r.add_delete("/_gw/api/accounts/{id}", self.delete_account)
        r.add_post("/v1/messages", self.messages)
        r.add_post("/v1/messages/count_tokens", self.messages)
        r.add_route("*", "/v1/{tail:.*}", self.other)
        r.add_route("*", "/api/{tail:.*}", self.other)
        return app

    async def _startup(self, app):
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)
        self.http = aiohttp.ClientSession(timeout=timeout)
        self.gateway = Gateway(self.store, self.picks, self.http)
        self._refresher = asyncio.create_task(self._refresh_loop())

    async def _cleanup(self, app):
        self._refresher.cancel()
        await self._stop_openai_listener()
        for d in self.device_invites.values():
            if d.get("task"):
                d["task"].cancel()
        await self.http.close()

    def _fetch_catalog_soon(self, account_id: str):
        account = self.store.get(account_id)
        if account:
            task = asyncio.create_task(self.gateway.refresh_catalog(account))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _refresh_loop(self):
        for account in self.store.list():
            if not account.get("catalog") and account["status"] != "needs_login":
                self._fetch_catalog_soon(account["id"])
        while True:
            try:
                await self.gateway.refresh_expiring()
            except Exception:
                log.exception("refresh sweep failed")
            await asyncio.sleep(config.REFRESH_SWEEP_EVERY_S)

    # ---- Claude Code traffic --------------------------------------------

    async def messages(self, request):
        return await self.gateway.messages(request)

    async def other(self, request):
        return await self.gateway.other(request)

    # ---- UI ----------------------------------------------------------------

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def state(self, request):
        return web.json_response({
            "port": config.PORT,
            "claude_command": claude_command(),
            "active": self.store.active_id,
            "accounts": [public_view(a) for a in self.store.list()],
            "windows": self._windows_view(),
            "presets": {k: {"label": p["label"], "auth": p["auth"], "base_url": p["base_url"],
                            "models": p["models"]} for k, p in providers.PRESETS.items()},
            "requests": list(self.gateway.requests)[:50],
            "invites": self.pending.invites("claude"),
            "device_invites": self._device_view(),
            "now": time.time(),
        })

    def _added(self, result: dict, provider: str) -> web.Response:
        account, replaced = self.store.upsert(provider, result["identity"], result["label"],
                                              result["credentials"], info=result["info"])
        log.info("%s account %s", "replaced" if replaced else "added", account["id"])
        self._fetch_catalog_soon(account["id"])
        q = urllib.parse.urlencode({"added": account["label"], "replaced": int(replaced)})
        raise web.HTTPFound(f"http://localhost:{config.PORT}/?{q}")

    @staticmethod
    def _failed(message: str):
        q = urllib.parse.urlencode({"error": message})
        raise web.HTTPFound(f"http://localhost:{config.PORT}/?{q}")

    # ---- Claude login --------------------------------------------------------

    async def claude_login(self, request):
        url = oauth.claude_authorize_url(self.pending, f"http://localhost:{config.PORT}/callback")
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

    async def create_invite(self, request):
        """An authorize link for someone else (or a browser on another machine).

        Anthropic's own redirect page shows them a ``code#state`` to send back;
        no callback to this machine is needed.
        """
        note = ((await request.json()).get("note") or "").strip()[:80]
        url = oauth.claude_authorize_url(self.pending, oauth.CLAUDE["manual_redirect_uri"], invite_note=note)
        return web.json_response({"url": url, "invites": self.pending.invites("claude")})

    async def cancel_invite(self, request):
        self.pending.discard(request.match_info["state"])
        return web.json_response({"invites": self.pending.invites("claude")})

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
                await web.TCPSite(runner, config.HOST, oauth.OPENAI["callback_port"]).start()
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

    def _llm_view(self, session_id: str) -> dict:
        default = self.store.get(self.store.active_id) if self.store.active_id else None
        return {
            "session": session_id,
            "pick": self.picks.get(session_id) if session_id else None,
            "default": self._llm_option(default) if default else None,
            "options": [self._llm_option(self.store.get(AS_IS))] + [self._llm_option(a) for a in self.store.list()],
            "ui": f"http://localhost:{config.PORT}",
        }

    async def llm_options(self, request):
        return web.json_response(self._llm_view(request.query.get("session", "")))

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

    # ---- the /llm mod: one-click install into Claude Code ---------------------

    async def _claude(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "claude", "plugin", *args, stdin=asyncio.subprocess.DEVNULL,
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

    async def mod_status(self, request):
        return web.json_response(await self._mod_view())

    async def mod_install(self, request):
        """Add the bundled marketplace (or refresh it), then install or update the mod."""
        steps = []
        code, out = await self._claude("marketplace", "list", "--json")
        known = code == 0 and any(m["name"] == MOD_MARKETPLACE for m in json.loads(out))
        step = ("marketplace", "update", MOD_MARKETPLACE) if known else ("marketplace", "add", str(MOD_DIR))
        for args in (step, ("install", f"{MOD_NAME}@{MOD_MARKETPLACE}")):
            code, out = await self._claude(*args)
            steps.append({"command": "claude plugin " + " ".join(args), "exit": code, "output": out[-800:]})
            if code != 0:
                return web.json_response({"ok": False, "steps": steps, **await self._mod_view()}, status=500)
        view = await self._mod_view()
        if view["installed"] != view["available"]:
            code, out = await self._claude("update", f"{MOD_NAME}@{MOD_MARKETPLACE}")
            steps.append({"command": f"claude plugin update {MOD_NAME}@{MOD_MARKETPLACE}", "exit": code,
                          "output": out[-800:]})
            view = await self._mod_view()
        log.info("mod install: %s", [(s["command"], s["exit"]) for s in steps])
        return web.json_response({"ok": view["installed"] == view["available"], "steps": steps, **view})

    def _windows_view(self) -> list[dict]:
        picks = self.picks.all()
        seen = self.gateway.sessions
        rows = []
        for sid in set(picks) | set(seen):
            s, pick = seen.get(sid, {}), picks.get(sid)
            if not s and time.time() - pick["picked_at"] > 86400:
                continue  # an old pick from a window that hasn't spoken since this gateway started
            picked = self.store.get(pick["account"]) if pick else None
            routed = self.store.get(s["account"]) if s.get("account") else None
            rows.append({"session": sid, "pick": pick["account"] if pick else None,
                         "pick_label": picked["label"] if picked else (pick["account"] if pick else None),
                         "routed_label": routed["label"] if routed else None, "route": s.get("route"),
                         "requests": s.get("requests", 0),
                         "last_seen": s.get("last_seen") or (pick or {}).get("used_at")})
        rows.sort(key=lambda r: -(r["last_seen"] or 0))
        return rows

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

    async def activate(self, request):
        self.store.set_active(self._account(request, allow_as_is=True)["id"])
        return web.json_response({"active": self.store.active_id})

    async def update_account(self, request):
        account = self._account(request)
        data = await request.json()
        options = {**account.get("options", {}), **data["options"]} if "options" in data else None
        self.store.update_settings(account["id"], label=data.get("label"), base_url=data.get("base_url"),
                                   models=data.get("models"), options=options)
        return web.json_response({"account": public_view(self.store.get(account["id"]))})

    async def delete_account(self, request):
        self.store.delete(self._account(request)["id"])
        return web.json_response({"active": self.store.active_id})

    async def refresh(self, request):
        account = self._account(request)
        if "refresh_token" not in account["credentials"]:
            return web.json_response({"error": "API-key accounts do not refresh"}, status=400)
        try:
            await self.gateway.credentials(account, force_refresh=True)
        except Exception as e:  # GatewayError or network trouble; show it in the UI
            return web.json_response({"error": getattr(e, "message", str(e))}, status=400)
        return web.json_response({"account": public_view(self.store.get(account["id"]))})

    async def refresh_models(self, request):
        catalog = await self.gateway.refresh_catalog(self._account(request))
        if catalog["error"]:
            return web.json_response({"error": catalog["error"]}, status=502)
        return web.json_response({"count": len(catalog["models"])})

    async def test_account(self, request):
        """Send a tiny Claude-Code-shaped request through this account."""
        account = self._account(request)
        body = {"model": "claude-haiku-4-5", "max_tokens": 64, "stream": False,
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
        if account["kind"] == "anthropic_oauth":
            body["system"] = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
        headers = {ACCOUNT_HEADER: account["id"], "anthropic-version": "2023-06-01"}
        started = time.monotonic()
        async with self.http.post(f"http://{config.HOST}:{config.PORT}/v1/messages",
                                  json=body, headers=headers) as r:
            result = await r.json(content_type=None)
        ms = int((time.monotonic() - started) * 1000)
        if r.status != 200:
            return web.json_response({"ok": False, "status": r.status, "ms": ms,
                                      "error": (result.get("error") or {}).get("message", result)})
        text = "".join(b.get("text", "") for b in result.get("content", []) if b["type"] == "text")
        return web.json_response({"ok": True, "ms": ms, "reply": text, "usage": result.get("usage")})


def claude_command() -> str:
    # --settings, not plain env vars: an ANTHROPIC_BASE_URL in ~/.claude/settings.json
    # (Voitta Desktop writes one) outranks the process environment.
    # No token: Claude Code keeps its own login, which "As is" forwards; every
    # other account ignores the client's credentials.
    env = {"ANTHROPIC_BASE_URL": f"http://localhost:{config.PORT}"}
    return f"claude --settings '{json.dumps({'env': env})}'"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)  # the UI polls every 3 s
    store = AccountStore(config.ACCOUNTS_FILE)
    app = App(store, SessionPicks(config.SESSIONS_FILE)).build()
    print(f"\n  Voitta LLM gateway POC on http://localhost:{config.PORT}\n"
          f"  Claude Code:  {claude_command()}\n")
    with contextlib.suppress(KeyboardInterrupt):
        web.run_app(app, host=config.HOST, port=config.PORT, print=None)
