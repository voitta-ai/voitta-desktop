"""Answer one Claude Code request from the account its window routes to.

The LLM proxy calls ``Router.route`` after its middleware has run. For "As is"
the answer is "not mine": the proxy forwards the request on its own path,
exactly as before. For any other account the router talks to the upstream and
hands back a ``Routed``: status, headers and a body stream already shaped like
Anthropic's, which the proxy streams through its middleware as usual.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from multidict import CIMultiDict

from . import config, models, oauth, providers, sse
from .common import OAI_SIGNATURE_PREFIX, Unsupported, estimate_tokens, portable_tools
from .store import AccountStore, SessionPicks
from .translate_chat import ChatStreamTranslator, to_chat_request
from .translate_responses import ResponsesStreamTranslator, to_responses_request

log = logging.getLogger("voitta-desktop.llm")

# Request headers never copied upstream. The client's auth is ignored: the
# router authenticates with the account instead.
_DROP_REQUEST = {"host", "authorization", "x-api-key", "content-length", "accept-encoding",
                 "connection", "keep-alive", "transfer-encoding", "origin", "cookie"}
# The router's session decompresses upstream bodies, so these would be wrong downstream.
_DROP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}

_CLAUDE_CODE_BETAS = (oauth.OAUTH_BETA, "claude-code-20250219")

# Every request Claude Code sends carries its session id; one window, one id
# (a /clear moves the window to a new one, which the mod re-picks).
SESSION_HEADER = "x-claude-code-session-id"

# What an Anthropic-compatible third party gets. Claude Code also sends
# Anthropic-only fields (output_config, context_management, and safeguards,
# which carries local paths and the user name); those stay here.
_COMPAT_FIELDS = {"model", "messages", "system", "max_tokens", "stop_sequences", "stream", "temperature",
                  "top_p", "top_k", "tools", "tool_choice", "metadata", "thinking"}

_HUNG_UP = "Claude Code closed the connection (interrupted?)"


def _header(headers: dict, name: str) -> str | None:
    name = name.lower()
    return next((v for k, v in headers.items() if k.lower() == name), None)


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


class GatewayError(Exception):
    def __init__(self, status: int, message: str, *, retry: bool = True):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry = retry

    def routed(self) -> "Routed":
        # Claude Code (the Anthropic SDK) honours x-should-retry; without it,
        # it retries a 429 up to 10 times.
        headers = {"Content-Type": "application/json"}
        if not self.retry:
            headers["x-should-retry"] = "false"
        return Routed.of_bytes(self.status, headers, json.dumps(sse.error_body(self.status, self.message)).encode())


class Routed:
    """An upstream answer, shaped like the aiohttp response the proxy streams:
    ``status``, ``headers``, ``content.iter_any()`` and ``read()``. The proxy
    must ``aclose()`` it when done, which also releases the upstream."""

    def __init__(self, status: int, headers, chunks: AsyncIterator[bytes]):
        self.status = status
        self.headers = CIMultiDict(headers)
        self.content = self
        self._chunks = chunks

    @classmethod
    def of_bytes(cls, status: int, headers, body: bytes) -> "Routed":
        async def once():
            yield body
        return cls(status, headers, once())

    def iter_any(self) -> AsyncIterator[bytes]:
        return self._chunks

    async def read(self) -> bytes:
        return b"".join([c async for c in self._chunks])

    async def aclose(self):
        await self._chunks.aclose()


@dataclass
class Decision:
    """What ``route`` decided. ``routed`` is None for "As is": the proxy
    forwards the request itself, then reports the status with ``as_is_done``.
    The proxy calls ``done`` once the answer has been sent (or abandoned)."""
    entry: dict
    routed: Routed | None = None
    on_done: Callable[[dict], None] | None = None
    _started: float = field(default_factory=time.monotonic)

    def as_is_done(self, status: int, host: str):
        self.entry.update(status=status, upstream={"host": host, "trace": None, "id": None, "model": None},
                          ms=int((time.monotonic() - self._started) * 1000))

    def done(self):
        if self.on_done:
            self.on_done(self.entry)


class RoutingJournal:
    """Per window, one line per model request: where it went and who answered.

    Claude Code's own transcript records the model it asked for; for a request
    routed to another account the real answer came from elsewhere, and only
    this journal knows. /voitta-store copies it beside the transcript.
    """

    KEEP_DAYS = 30

    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, session_id: str) -> Path:
        return self.folder / f"{session_id}.jsonl"

    def append(self, entry: dict):
        session_id = entry.get("session_id")
        if not session_id or entry.get("path") != "/v1/messages" or not _SAFE_ID.fullmatch(session_id):
            return
        record = {k: entry.get(k) for k in ("ts", "status", "ms", "route", "account_id", "account", "provider",
                                           "model", "upstream_model", "upstream", "usage", "error", "notes")
                  if entry.get(k) is not None}
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path(session_id), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("routing journal for %s: %s", session_id[:8], e)

    def read(self, session_id: str) -> list[dict]:
        try:
            lines = self.path(session_id).read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def prune(self):
        cutoff = time.time() - self.KEEP_DAYS * 86400
        for p in self.folder.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


# Claude Code session ids are UUIDs; anything else never names a file.
_SAFE_ID = re.compile(r"[0-9A-Za-z-]{8,64}")


def _strip_foreign_thinking(body: dict) -> bool:
    """Drop thinking blocks Anthropic would reject: the router's own (made
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


class Router:
    def __init__(self, store: AccountStore, picks: SessionPicks, http: aiohttp.ClientSession,
                 *, ui_url: str, self_origins: set[str], journal: RoutingJournal | None = None):
        self.store = store
        self.picks = picks
        self.http = http
        self.journal = journal
        self.ui_url = ui_url
        self.self_origins = self_origins
        self.requests = deque(maxlen=config.REQUEST_LOG_SIZE)
        # Windows seen since start, for the UI: session id -> counters.
        self.sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _relogin(self, account: dict) -> str:
        return (f"{account['label']}: the login has expired; add the account again in Voitta Desktop "
                f"(Settings → LLMs, {self.ui_url}).")

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
                raise GatewayError(403, self._relogin(account)) from e
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

    # ---- routing -----------------------------------------------------------

    def resolve(self, session_id: str | None) -> tuple[dict, str]:
        """The account a request goes to, and why: ``window`` (the window's
        /llm pick) or ``default``. Never a fallback: a pick or default that
        can't be used is an error."""
        if session_id and (pick := self.picks.get(session_id)):
            account, route = self.store.get(pick), "window"
            if not account:
                raise GatewayError(409, f"The account this window picked with /llm ({pick}) no longer exists. "
                                        "Run /llm to pick another.", retry=False)
            self.picks.touch(session_id)
        else:
            default = self.store.active_id
            account, route = (self.store.get(default) if default else None), "default"
            if not account:
                raise GatewayError(403, f"No default LLM account. Choose one in Voitta Desktop "
                                        f"(Settings → LLMs, {self.ui_url}).", retry=False)
        if account["status"] == "needs_login":
            raise GatewayError(403, self._relogin(account), retry=False)
        return account, route

    def _journal_done(self, entry: dict):
        if self.journal:
            self.journal.append(entry)

    def _seen(self, session_id: str | None, account: dict, route: str):
        if not session_id:
            return
        s = self.sessions.setdefault(session_id, {"first_seen": time.time(), "requests": 0})
        s.update(last_seen=time.time(), account=account["id"], route=route)
        s["requests"] += 1

    def _entry(self, path: str, session_id: str | None) -> dict:
        entry = {"ts": time.time(), "path": path.split("?")[0], "status": None,
                 "session": session_id[:8] if session_id else None, "session_id": session_id}
        self.requests.appendleft(entry)
        return entry

    async def route(self, method: str, path: str, headers: dict, body: bytes) -> Decision:
        """Decide where one proxied request goes and, unless that is "As is", answer it."""
        session_id = _header(headers, SESSION_HEADER)
        entry = self._entry(path, session_id)
        try:
            account, route = self.resolve(session_id)
        except GatewayError as e:
            entry.update(status=e.status, error=e.message, ms=0)
            return Decision(entry, e.routed(), self._journal_done)
        entry.update(account=account["label"], account_id=account["id"], provider=account["provider"], route=route)
        self._seen(session_id, account, route)
        if account["kind"] == "as_is":
            # The default path, so no JSON parse: Claude Code puts "model" first.
            if m := _MODEL_RE.search(body, 0, 512):
                entry["model"] = m.group(1).decode("utf-8", "replace")
            return Decision(entry, None, self._journal_done)
        # A web page must not be able to spend the user's accounts through this
        # local port; Claude Code sends no Origin, a browser always does.
        origin = _header(headers, "origin")
        if origin and origin not in self.self_origins:
            entry.update(status=403, error=f"cross-origin request from {origin} refused", ms=0)
            return Decision(entry, GatewayError(403, "cross-origin request refused", retry=False).routed(),
                            self._journal_done)
        return Decision(entry, await self.answer(account, method, path, headers, body, entry), self._journal_done)

    async def answer(self, account: dict, method: str, path: str, headers: dict, body: bytes,
                     entry: dict) -> Routed:
        """The upstream's answer for ``account``. Errors become Anthropic-shaped
        error answers; nothing is ever sent to a different account."""
        started = time.monotonic()
        try:
            if account["kind"] in ("anthropic_oauth", "anthropic_compat"):
                return await self._passthrough(account, method, path, headers, body, entry)
            bare = path.split("?")[0]
            if not bare.startswith("/v1/messages"):
                raise GatewayError(404, f"{bare} is not available for this account", retry=False)
            data = json.loads(body)
            entry.update(model=data.get("model"), stream=bool(data.get("stream")))
            if bare.endswith("/count_tokens"):
                entry["status"] = 200
                return Routed.of_bytes(200, {"Content-Type": "application/json"},
                                       json.dumps({"input_tokens": estimate_tokens(data)}).encode())
            portable_tools(data, entry.setdefault("notes", []))
            return await self._translated(account, data, entry)
        except GatewayError as e:
            entry.update(status=e.status, error=e.message)
            return e.routed()
        except Unsupported as e:
            entry.update(status=400, error=str(e))
            return GatewayError(400, str(e), retry=False).routed()
        except aiohttp.ClientError as e:
            entry.update(status=502, error=str(e))
            return GatewayError(502, f"upstream connection failed: {e}").routed()
        finally:
            entry.setdefault("ms", int((time.monotonic() - started) * 1000))

    # ---- Anthropic-shaped upstreams ------------------------------------

    async def _passthrough(self, account, method, path, headers, raw: bytes, entry) -> Routed:
        headers = {k: v for k, v in headers.items() if k.lower() not in _DROP_REQUEST}
        native = account["kind"] == "anthropic_oauth"
        if native:
            betas = [b.strip() for b in (_header(headers, "anthropic-beta") or "").split(",") if b.strip()]
            headers = {k: v for k, v in headers.items() if k.lower() != "anthropic-beta"}
            betas += [b for b in _CLAUDE_CODE_BETAS if b not in betas]
            headers["anthropic-beta"] = ",".join(betas)
        else:
            headers = {k: v for k, v in headers.items() if k.lower() != "anthropic-beta"}

        if raw and method == "POST":
            if native:
                # Only parse when there is something to strip; most requests go through untouched.
                if OAI_SIGNATURE_PREFIX.encode() in raw or b'"signature":""' in raw:
                    body = json.loads(raw)
                    if _strip_foreign_thinking(body):
                        raw = json.dumps(body).encode()
                    entry["model"] = body.get("model")
                elif b'"model"' in raw:
                    entry["model"] = json.loads(raw).get("model")
            else:
                body = json.loads(raw)
                _strip_foreign_thinking(body)
                if path.startswith("/v1/messages"):
                    body = _compat_body(body)
                    portable_tools(body, entry.setdefault("notes", []))
                entry["model"] = body.get("model")
                if "model" in body:
                    body["model"] = providers.map_model(account, body["model"])
                    entry["upstream_model"] = body["model"]
                raw = json.dumps(body).encode()

        url = account["base_url"].rstrip("/") + (path if native else path.split("?")[0])
        for attempt in (0, 1):
            creds = await self.credentials(account, force_refresh=attempt == 1)
            if native:
                headers["Authorization"] = f"Bearer {creds['access_token']}"
            else:
                headers["x-api-key"] = creds["api_key"]
                headers["Authorization"] = f"Bearer {creds['api_key']}"
            resp = await self.http.request(method, url, data=raw or None, headers=headers)
            if resp.status == 401 and native and attempt == 0:
                resp.release()
                continue
            break
        entry["status"] = resp.status
        entry["upstream"] = receipt = _receipt(resp)
        if resp.status >= 400:
            try:
                text = await resp.text()
            finally:
                resp.release()
            entry["error"] = text[:500]
            if resp.status == 401:
                raise GatewayError(403, f"{account['label']} rejected the credentials: {text[:500]}")
            return Routed.of_bytes(resp.status, {"Content-Type": "application/json"}, text.encode())
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESPONSE}
        return Routed(resp.status, out_headers, self._relay(resp, receipt, entry))

    async def _relay(self, resp: aiohttp.ClientResponse, receipt: dict, entry: dict):
        head = b""
        try:
            async for chunk in resp.content.iter_any():
                if len(head) < 4096:
                    head += chunk
                    _sniff(receipt, head)
                yield chunk
        except GeneratorExit:
            entry.update(status=499, error=_HUNG_UP)
            raise
        except asyncio.CancelledError:
            entry.update(status=499, error=_HUNG_UP)
            raise
        finally:
            resp.release()

    # ---- translated upstreams -------------------------------------------

    async def _translated(self, account, body: dict, entry: dict) -> Routed:
        upstream_model = providers.map_model(account, body["model"])
        entry["upstream_model"] = upstream_model
        resp = await self._open_translated(account, body, upstream_model)
        entry["upstream"] = _receipt(resp)
        if account["kind"] == "openai_codex":
            translator = ResponsesStreamTranslator(body["model"])
        else:
            translator = ChatStreamTranslator(body["model"])
        events = self._translate(resp, translator, entry["upstream"])
        if body.get("stream"):
            entry["status"] = 200
            return Routed(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                          self._stream_out(resp, events, entry, translator))
        try:
            builder = sse.MessageBuilder()
            async for ev in events:
                if ev["type"] == "error":
                    raise GatewayError(502, ev["error"]["message"])
                builder.feed(ev)
        finally:
            resp.release()
        entry.update(status=200, usage=translator.usage)
        return Routed.of_bytes(200, {"Content-Type": "application/json"}, json.dumps(builder.result()).encode())

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
        return GatewayError(429, f"{account['label']}: {detail}. Switch accounts with /llm or in "
                                 f"Voitta Desktop (Settings → LLMs).", retry=False)

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

    async def _stream_out(self, resp, events, entry, translator):
        try:
            try:
                async for ev in events:
                    if ev["type"] == "error":
                        entry["error"] = ev["error"]["message"]
                    yield sse.encode(ev)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                entry["error"] = f"upstream stream broke: {e}"
                yield sse.encode(sse.error_body(502, entry["error"]))
            entry["usage"] = translator.usage
        except GeneratorExit:
            entry.update(status=499, error=_HUNG_UP)
            raise
        except asyncio.CancelledError:
            entry.update(status=499, error=_HUNG_UP)
            raise
        finally:
            resp.release()
