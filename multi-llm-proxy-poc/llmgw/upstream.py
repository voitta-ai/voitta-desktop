"""Forward one Claude Code request to the account its window routes to."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque

import aiohttp
from aiohttp import web

from . import config, models, oauth, providers, sse
from .common import OAI_SIGNATURE_PREFIX, Unsupported, estimate_tokens, portable_tools
from .store import AS_IS, AccountStore, SessionPicks
from .translate_chat import ChatStreamTranslator, to_chat_request
from .translate_responses import ResponsesStreamTranslator, to_responses_request

log = logging.getLogger("llmgw")

# Request headers never copied upstream. The client's auth is ignored: the
# gateway authenticates with the active account instead.
_DROP_REQUEST = {"host", "authorization", "x-api-key", "content-length", "accept-encoding",
                 "connection", "keep-alive", "transfer-encoding", "origin", "cookie"}
# aiohttp decompresses upstream bodies, so these would be wrong downstream.
_DROP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}

# "As is" forwards the client's own credentials; only hop-by-hop headers go.
_DROP_REQUEST_AS_IS = _DROP_REQUEST - {"authorization", "x-api-key"}

_CLAUDE_CODE_BETAS = (oauth.OAUTH_BETA, "claude-code-20250219")

# Every request Claude Code sends carries its session id; one window, one id
# (a /clear or resume moves the window to a new one, which the mod re-picks).
SESSION_HEADER = "x-claude-code-session-id"
# The UI's Test button pins a request to one account.
ACCOUNT_HEADER = "x-llmgw-account"
_ANTHROPIC_KINDS = ("as_is", "anthropic_oauth", "anthropic_compat")

# What an Anthropic-compatible third party gets. Claude Code also sends
# Anthropic-only fields (output_config, context_management, and safeguards,
# which carries local paths and the user name); those stay here.
_COMPAT_FIELDS = {"model", "messages", "system", "max_tokens", "stop_sequences", "stream", "temperature",
                  "top_p", "top_k", "tools", "tool_choice", "metadata", "thinking"}


def _compat_body(body: dict) -> dict:
    out = {k: v for k, v in body.items() if k in _COMPAT_FIELDS}
    if (out.get("thinking") or {}).get("type") != "enabled":
        out.pop("thinking", None)  # "adaptive" is Anthropic-only
    for t in out.get("tools") or []:
        if "input_schema" not in t:
            raise Unsupported(f"tool {t.get('name') or t.get('type')!r} is an Anthropic server tool; "
                              "this account's provider cannot run it")
    return out


_TRACE_HEADERS = ("request-id", "x-request-id", "x-ds-trace-id", "openai-request-id", "x-oai-request-id")
_ID_RE = re.compile(rb'"id"\s*:\s*"([^"]{1,120})"')
_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]{1,120})"')


def _receipt(resp: aiohttp.ClientResponse) -> dict:
    """Who actually answered, as the upstream itself reports it."""
    trace = next((resp.headers[h] for h in _TRACE_HEADERS if h in resp.headers), None)
    return {"host": resp.url.host, "trace": trace, "id": None, "model": None}


def _sniff(receipt: dict, head: bytes):
    """Fill id/model from the start of an Anthropic-shaped body (message_start or the JSON message)."""
    if receipt["id"] is None and (m := _ID_RE.search(head)):
        receipt["id"] = m.group(1).decode("utf-8", "replace")
    if receipt["model"] is None and (m := _MODEL_RE.search(head)):
        receipt["model"] = m.group(1).decode("utf-8", "replace")


_HUNG_UP = "Claude Code closed the connection (interrupted?)"


class ClientGone(Exception):
    """Claude Code closed the connection while we were still answering
    (usually the user interrupted). Not an upstream failure."""


async def _send(out: web.StreamResponse, data: bytes | None):
    """Write to Claude Code; a closed connection becomes ClientGone so it is
    never mistaken for the upstream failing. ``None`` ends the response."""
    try:
        await (out.write(data) if data is not None else out.write_eof())
    except (ConnectionResetError, aiohttp.ClientConnectionResetError) as e:
        raise ClientGone(str(e)) from e


class GatewayError(Exception):
    def __init__(self, status: int, message: str, *, retry: bool = True):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry = retry

    def response(self) -> web.Response:
        # Claude Code (the Anthropic SDK) honours x-should-retry; without it,
        # it retries a 429 up to 10 times.
        headers = {} if self.retry else {"x-should-retry": "false"}
        return web.json_response(sse.error_body(self.status, self.message), status=self.status, headers=headers)


def _strip_foreign_thinking(body: dict) -> bool:
    """Drop thinking blocks Anthropic would reject: the gateway's own (made
    from OpenAI reasoning) and unsigned ones. Returns whether anything changed."""
    changed = False
    for msg in body.get("messages", []):
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        kept = [b for b in msg["content"]
                if not (b.get("type") == "thinking"
                        and (not b.get("signature") or b["signature"].startswith(OAI_SIGNATURE_PREFIX)))]
        if len(kept) != len(msg["content"]):
            if not kept:
                raise Unsupported("an earlier assistant turn holds only ChatGPT reasoning, which Anthropic "
                                  "cannot accept; start a new conversation for this account")
            msg["content"] = kept
            changed = True
    return changed


class Gateway:
    def __init__(self, store: AccountStore, picks: SessionPicks, http: aiohttp.ClientSession):
        self.store = store
        self.picks = picks
        self.http = http
        self.requests = deque(maxlen=config.REQUEST_LOG_SIZE)
        # Windows seen since start, for the UI: session id -> counters.
        self.sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- credentials -----------------------------------------------------

    async def credentials(self, account: dict, *, force_refresh=False) -> dict:
        if "api_key" in account["credentials"]:
            return account["credentials"]
        lock = self._locks.setdefault(account["id"], asyncio.Lock())
        async with lock:
            account = self.store.get(account["id"]) or account
            creds = account["credentials"]
            if not force_refresh and creds["expires_at"] - time.time() > config.REFRESH_MARGIN_S:
                return creds
            try:
                new = await oauth.refresh(self.http, account)
            except oauth.RefreshRejected as e:
                self.store.set_status(account["id"], "needs_login", str(e)[:200])
                raise GatewayError(403, f"{account['label']}: the login has expired; add the account again "
                                        f"in the gateway UI (http://localhost:{config.PORT}).") from e
            except (oauth.OAuthError, aiohttp.ClientError) as e:
                self.store.set_status(account["id"], "error", f"refresh failed: {e}"[:200])
                raise GatewayError(503, f"{account['label']}: token refresh failed: {e}") from e
            self.store.update_credentials(account["id"], new)
            log.info("refreshed %s", account["id"])
            return new

    async def refresh_expiring(self):
        """Background sweep so idle accounts don't run out between requests."""
        horizon = time.time() + config.REFRESH_SWEEP_AHEAD_S
        for account in self.store.list():
            creds = account["credentials"]
            if "refresh_token" in creds and account["status"] != "needs_login" and creds["expires_at"] < horizon:
                try:
                    await self.credentials(account, force_refresh=True)
                except GatewayError as e:
                    log.warning("background refresh of %s failed: %s", account["id"], e.message)

    async def refresh_catalog(self, account: dict) -> dict:
        try:
            creds = await self.credentials(account)
            catalog = models.stamp(await models.fetch(self.http, account, creds))
        except (GatewayError, models.CatalogError, aiohttp.ClientError, ValueError, KeyError) as e:
            message = getattr(e, "message", None) or str(e) or type(e).__name__
            log.warning("model list for %s failed: %s", account["id"], message)
            old = (self.store.get(account["id"]) or {}).get("catalog") or {}
            catalog = {**models.stamp(old.get("models")), "error": message[:300]}
        self.store.set_catalog(account["id"], catalog)
        self.store.fill_models(account["id"], catalog["suggested"])
        return catalog

    def resolve(self, request: web.Request) -> tuple[dict, str]:
        """The account this request goes to, and why: ``pinned`` (UI test),
        ``window`` (the window's /llm pick) or ``default``. Never a fallback:
        a pick or default that can't be used is an error."""
        pinned = request.headers.get(ACCOUNT_HEADER)
        session_id = request.headers.get(SESSION_HEADER)
        if pinned:
            account, route = self.store.get(pinned), "pinned"
            if not account:
                raise GatewayError(404, f"No account {pinned!r}.", retry=False)
        elif session_id and (pick := self.picks.get(session_id)):
            account, route = self.store.get(pick), "window"
            if not account:
                raise GatewayError(409, f"The account this window picked with /llm ({pick}) no longer exists. "
                                        "Run /llm to pick another.", retry=False)
            self.picks.touch(session_id)
        else:
            default = self.store.active_id
            account, route = (self.store.get(default) if default else None), "default"
            if not account:
                raise GatewayError(403, f"No default account. Choose one at http://localhost:{config.PORT}",
                                   retry=False)
        if account["status"] == "needs_login":
            raise GatewayError(403, f"{account['label']}: the login has expired; add the account again "
                                    f"in the gateway UI (http://localhost:{config.PORT}).", retry=False)
        return account, route

    def _seen(self, session_id: str | None, account: dict, route: str):
        if not session_id:
            return
        s = self.sessions.setdefault(session_id, {"first_seen": time.time(), "requests": 0})
        s.update(last_seen=time.time(), account=account["id"], route=route)
        s["requests"] += 1

    # ---- entry points ----------------------------------------------------

    async def messages(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        started = time.monotonic()
        session_id = request.headers.get(SESSION_HEADER)
        entry = {"ts": time.time(), "path": request.path, "status": None,
                 "session": session_id[:8] if session_id else None}
        self.requests.appendleft(entry)
        try:
            account, route = self.resolve(request)
            entry.update(account=account["label"], provider=account["provider"], route=route)
            self._seen(session_id, account, route)
            if account["kind"] in _ANTHROPIC_KINDS:
                return await self._passthrough(request, account, raw, entry)
            body = json.loads(raw)
            entry.update(model=body.get("model"), stream=bool(body.get("stream")))
            if request.path.endswith("/count_tokens"):
                entry["status"] = 200
                return web.json_response({"input_tokens": estimate_tokens(body)})
            portable_tools(body, entry.setdefault("notes", []))
            return await self._translated(request, account, body, entry)
        except GatewayError as e:
            entry.update(status=e.status, error=e.message)
            return e.response()
        except Unsupported as e:
            entry.update(status=400, error=str(e))
            return GatewayError(400, str(e), retry=False).response()
        except ClientGone:
            # 499: nginx's "client closed request". Nothing can be sent back.
            entry.update(status=499, error=_HUNG_UP)
            return web.Response(status=499)
        except asyncio.CancelledError:
            # aiohttp may instead cancel the handler when the client goes away.
            entry.update(status=499, error=_HUNG_UP)
            raise
        except aiohttp.ClientError as e:
            entry.update(status=502, error=str(e))
            return GatewayError(502, f"upstream connection failed: {e}").response()
        finally:
            entry["ms"] = int((time.monotonic() - started) * 1000)

    async def other(self, request: web.Request) -> web.StreamResponse:
        """Any other Anthropic path Claude Code calls (/v1/models, /api/...)."""
        try:
            account, _ = self.resolve(request)
        except GatewayError as e:
            return e.response()
        if account["kind"] in ("as_is", "anthropic_oauth"):
            return await self.messages(request)
        return web.json_response(sse.error_body(404, f"{request.path} is not available for this account"),
                                 status=404)

    # ---- Anthropic-shaped upstreams ------------------------------------

    async def _passthrough(self, request, account, raw: bytes, entry: dict) -> web.StreamResponse:
        as_is = account["kind"] == "as_is"
        drop = _DROP_REQUEST_AS_IS if as_is else _DROP_REQUEST
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        native = account["kind"] in ("anthropic_oauth", "as_is")
        if as_is:
            pass  # betas and auth exactly as Claude Code sent them
        elif native:
            betas = [b.strip() for b in headers.pop("anthropic-beta", "").split(",") if b.strip()]
            betas += [b for b in _CLAUDE_CODE_BETAS if b not in betas]
            headers["anthropic-beta"] = ",".join(betas)
        else:
            headers.pop("anthropic-beta", None)

        if raw and request.method == "POST":
            if native:
                # Only parse when there is something to strip; most requests go through untouched.
                if OAI_SIGNATURE_PREFIX.encode() in raw or b'"signature":""' in raw:
                    body = json.loads(raw)
                    if _strip_foreign_thinking(body):
                        raw = json.dumps(body).encode()
                    entry["model"] = body.get("model")
            else:
                body = json.loads(raw)
                _strip_foreign_thinking(body)
                if request.path.startswith("/v1/messages"):
                    body = _compat_body(body)
                    portable_tools(body, entry.setdefault("notes", []))
                entry["model"] = body.get("model")
                if "model" in body:
                    body["model"] = providers.map_model(account, body["model"])
                    entry["upstream_model"] = body["model"]
                raw = json.dumps(body).encode()

        url = account["base_url"].rstrip("/") + (request.path_qs if native else request.path)
        for attempt in (0, 1):
            if as_is:
                resp = await self.http.request(request.method, url, data=raw or None, headers=headers)
                break
            creds = await self.credentials(account, force_refresh=attempt == 1)
            if native:
                headers["Authorization"] = f"Bearer {creds['access_token']}"
            else:
                headers["x-api-key"] = creds["api_key"]
                headers["Authorization"] = f"Bearer {creds['api_key']}"
            resp = await self.http.request(request.method, url, data=raw or None, headers=headers)
            if resp.status == 401 and native and attempt == 0:
                resp.release()
                continue
            break
        try:
            entry["status"] = resp.status
            entry["upstream"] = receipt = _receipt(resp)
            if resp.status >= 400:
                text = await resp.text()
                entry["error"] = text[:500]
                # As is: a 401 goes back unchanged so Claude Code renews its own login.
                status = 403 if resp.status == 401 and not as_is else resp.status
                return web.Response(status=status, text=text if status == resp.status else json.dumps(
                    sse.error_body(403, f"{account['label']} rejected the credentials: {text[:500]}")),
                    content_type="application/json")
            out = web.StreamResponse(status=resp.status, headers={
                k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESPONSE})
            await out.prepare(request)
            head = b""
            async for chunk in resp.content.iter_any():
                if len(head) < 4096:
                    head += chunk
                    _sniff(receipt, head)
                await _send(out, chunk)
            await _send(out, None)
            return out
        finally:
            resp.release()

    # ---- translated upstreams -------------------------------------------

    async def _translated(self, request, account, body: dict, entry: dict) -> web.StreamResponse:
        upstream_model = providers.map_model(account, body["model"])
        entry["upstream_model"] = upstream_model
        resp = await self._open_translated(account, body, upstream_model)
        entry["upstream"] = _receipt(resp)
        try:
            if account["kind"] == "openai_codex":
                translator = ResponsesStreamTranslator(body["model"])
            else:
                translator = ChatStreamTranslator(body["model"])
            events = self._translate(resp, translator, entry["upstream"])
            if body.get("stream"):
                return await self._stream_out(request, events, entry, translator)
            builder = sse.MessageBuilder()
            async for ev in events:
                if ev["type"] == "error":
                    raise GatewayError(502, ev["error"]["message"])
                builder.feed(ev)
            entry.update(status=200, usage=translator.usage)
            return web.json_response(builder.result())
        finally:
            resp.release()

    async def _open_translated(self, account, body, upstream_model) -> aiohttp.ClientResponse:
        for attempt in (0, 1):
            creds = await self.credentials(account, force_refresh=attempt == 1)
            if account["kind"] == "openai_codex":
                req = to_responses_request(body, account, upstream_model)
                url = account["base_url"].rstrip("/") + "/responses"
                headers = {
                    "Authorization": f"Bearer {creds['access_token']}",
                    "chatgpt-account-id": creds.get("account_id") or "",
                    "OpenAI-Beta": "responses=experimental",
                    "originator": "codex_cli_rs",
                    "session_id": req.get("prompt_cache_key") or str(uuid.uuid4()),
                    "Accept": "text/event-stream",
                }
            else:
                req = to_chat_request(body, account, upstream_model)
                url = account["base_url"].rstrip("/") + "/chat/completions"
                headers = {"Authorization": f"Bearer {creds['api_key']}", "Accept": "text/event-stream"}
            resp = await self.http.post(url, json=req, headers=headers)
            if resp.status == 200:
                if account["status"] == "limited":
                    self.store.set_status(account["id"], "ok")
                return resp
            text = await resp.text()
            resp.release()
            if resp.status == 401 and "refresh_token" in creds and attempt == 0:
                continue
            log.warning("%s %s -> %s: %s", account["id"], url, resp.status, text[:1000])
            if resp.status == 429:
                raise self._limit_error(account, upstream_model, text)
            status = 403 if resp.status == 401 else resp.status
            raise GatewayError(status, f"{account['provider']} ({upstream_model}) returned "
                                       f"{resp.status}: {text[:2000]}")

    def _limit_error(self, account, upstream_model, text) -> GatewayError:
        """A 429. A quota that resets in more than a minute is not worth Claude
        Code's retries: say so, mark the account, and tell the client to stop."""
        try:
            err = json.loads(text).get("error") or {}
        except ValueError:
            err = {}
        wait = err.get("resets_in_seconds")
        if not isinstance(wait, (int, float)) or wait <= 60:
            return GatewayError(429, f"{account['label']} ({upstream_model}): rate limited: {text[:500]}")
        resets = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + wait))
        detail = f"{err.get('message') or err.get('type') or 'usage limit reached'}; resets {resets}"
        self.store.set_status(account["id"], "limited", detail)
        return GatewayError(429, f"{account['label']}: {detail}. Switch accounts at "
                                 f"http://localhost:{config.PORT}", retry=False)

    async def _translate(self, resp, translator, receipt):
        for ev in translator.start():
            yield ev
        async for _event, data in sse.iter_sse(resp):
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if receipt["id"] is None:
                src = chunk.get("response") or chunk  # Responses: response.created; Chat: every chunk
                receipt["id"], receipt["model"] = src.get("id"), src.get("model")
            for ev in translator.feed(chunk):
                yield ev
        for ev in translator.finish():
            yield ev

    async def _stream_out(self, request, events, entry, translator) -> web.StreamResponse:
        out = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await out.prepare(request)
        entry["status"] = 200
        try:
            async for ev in events:
                if ev["type"] == "error":
                    entry["error"] = ev["error"]["message"]
                await _send(out, sse.encode(ev))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            entry["error"] = f"upstream stream broke: {e}"
            await _send(out, sse.encode(sse.error_body(502, entry["error"])))
        entry["usage"] = translator.usage
        await _send(out, None)
        return out
