"""Request logger middleware — writes every request/response to JSONL."""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from paths import LOG_DIR

from .base import Middleware, ProxyRequest, ProxyResponse, decompress

logger = logging.getLogger("voitta-desktop.logger")


class RequestLogger(Middleware):
    """Logs every request and response to a JSONL file, one file per day."""

    def __init__(
        self,
        log_dir: Path = LOG_DIR,
        stale_after_s: int = 60,
        watchdog_interval_s: int = 30,
        clear_on_start: bool = True,
        keep_messages: int = 2,
        max_str: int = 2000,
        rss_log_step_mb: float = 250.0,
    ):
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        if clear_on_start:
            self._clear_logs()
        self._keep_messages = keep_messages
        self._max_str = max_str
        self._pending: dict[int, dict] = {}
        self._stale_after_s = stale_after_s
        self._watchdog_interval_s = watchdog_interval_s
        self._rss_log_step_mb = rss_log_step_mb
        self._last_logged_rss_mb = 0.0
        self._lock = threading.Lock()
        # The watchdog is a coroutine, spawned by AppBase on the shared
        # runtime — see runtime.py. It used to be its own thread.

    def _clear_logs(self) -> None:
        """Delete all request-log JSONL files from a previous run.

        Scoped to the ``*.jsonl`` files this logger owns; the app's own
        ``desktop.log`` (already size-capped) is left untouched.
        """
        freed = 0
        removed = 0
        for path in self._log_dir.glob("*.jsonl"):
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except OSError as e:
                logger.warning("Failed to remove old log %s: %s", path, e)
        if removed:
            logger.info("Cleared %d old request log(s), freed %.1f MB",
                        removed, freed / 1_000_000)

    def _truncate(self, obj):
        """Recursively cap long strings; returns a new structure (no mutation)."""
        if isinstance(obj, str):
            if len(obj) <= self._max_str:
                return obj
            return obj[:self._max_str] + f"...[+{len(obj) - self._max_str} chars]"
        if isinstance(obj, list):
            return [self._truncate(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._truncate(v) for k, v in obj.items()}
        return obj

    def _trim_request_body(self, body):
        """Build a logging-only copy of the request body, dropping the bulk.

        The expensive part is ``messages`` (the full conversation, re-sent on
        every call → O(N²) growth) and ``tools`` (identical schemas repeated
        each call). We keep only the last ``keep_messages`` messages plus a
        placeholder for the rest, reduce tools to their names, and truncate any
        remaining long strings. The original ``body`` is never mutated — it is
        still forwarded upstream untouched.
        """
        if not isinstance(body, dict):
            return self._truncate(body)

        trimmed = dict(body)  # shallow copy; only reassign the heavy keys

        msgs = body.get("messages")
        if isinstance(msgs, list):
            keep = self._keep_messages
            if len(msgs) <= keep:
                trimmed["messages"] = self._truncate(msgs)
            else:
                omitted = len(msgs) - keep
                placeholder = {
                    "role": "_omitted",
                    "content": f"[{omitted} earlier message(s) omitted; "
                               f"{len(msgs)} total]",
                }
                trimmed["messages"] = [placeholder] + self._truncate(msgs[-keep:])

        tools = body.get("tools")
        if isinstance(tools, list):
            trimmed["tools"] = {
                "_count": len(tools),
                "names": [t.get("name") for t in tools if isinstance(t, dict)],
            }

        system = body.get("system")
        if system is not None:
            trimmed["system"] = self._truncate(system)

        return trimmed

    def _log_path(self) -> Path:
        return self._log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        started_at = time.time()
        request_body = request.require_json() if request.body else None
        entry = {
            "timestamp": time.time(),
            "method": request.method,
            "path": request.path,
            "request_headers": {k: v for k, v in request.headers.items()
                                if k.lower() not in ("x-api-key", "authorization", "cookie")},
            "request_body": self._trim_request_body(request_body),
            "started_at": started_at,
            "last_activity_at": started_at,
            "chunk_count": 0,
            "response_bytes": 0,
            "watchdog_last_log_at": 0.0,
        }
        with self._lock:
            self._pending[id(request)] = entry
        logger.info("Request started: %s %s", request.method, request.path)
        return request

    async def on_response_started(self, request: ProxyRequest, response: ProxyResponse) -> ProxyResponse:
        req_id = id(request)
        with self._lock:
            entry = self._pending.get(req_id)
            if entry is not None:
                entry["response_status"] = response.status
                entry["response_headers"] = dict(response.headers)
                entry["encoding"] = response.headers.get("Content-Encoding", "")
                entry["last_activity_at"] = time.time()
                logger.info(
                    "Response started: %s %s -> %s (%s)",
                    entry["method"],
                    entry["path"],
                    response.status,
                    response.headers.get("Content-Type", "unknown"),
                )
        return response

    async def on_response_chunk(self, request: ProxyRequest, chunk: bytes) -> bytes:
        req_id = id(request)
        with self._lock:
            entry = self._pending.get(req_id)
            if entry is not None:
                entry.setdefault("chunks", []).append(chunk)
                entry["chunk_count"] += 1
                entry["response_bytes"] += len(chunk)
                entry["last_activity_at"] = time.time()
                if entry["chunk_count"] == 1:
                    logger.info(
                        "First response chunk: %s %s (%d bytes)",
                        entry["method"],
                        entry["path"],
                        len(chunk),
                    )
        return chunk

    async def on_response_done(self, request: ProxyRequest, response: ProxyResponse):
        req_id = id(request)
        with self._lock:
            entry = self._pending.pop(req_id, None)
        if not entry:
            return

        raw = b"".join(entry.pop("chunks", []))
        encoding = entry.pop("encoding", "")
        response_text = decompress(raw, encoding)

        if "text/event-stream" in entry.get("response_headers", {}).get("Content-Type", ""):
            # Parse SSE events, extract usage from final message_delta
            response_body = {}
            for line in response_text.split("\n"):
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except (json.JSONDecodeError, ValueError):
                    continue
                # message_start has model/id, message_delta has usage
                etype = event.get("type", "")
                if etype == "message_start":
                    msg = event.get("message", {})
                    response_body["model"] = msg.get("model")
                    response_body["id"] = msg.get("id")
                    response_body["usage"] = msg.get("usage", {})
                elif etype == "message_delta":
                    # Merge delta usage (has output_tokens) into usage
                    delta_usage = event.get("usage", {})
                    if delta_usage:
                        response_body.setdefault("usage", {}).update(delta_usage)
            entry["response_body"] = response_body
        else:
            try:
                entry["response_body"] = self._truncate(json.loads(response_text)) if response_text else None
            except (json.JSONDecodeError, ValueError):
                entry["response_body"] = response_text[:self._max_str] if response_text else None

        entry["duration_ms"] = int((time.time() - entry["timestamp"]) * 1000)
        logger.info(
            "Request finished: %s %s -> %s in %d ms (%d chunks, %d bytes)",
            entry["method"],
            entry["path"],
            entry.get("response_status", response.status),
            entry["duration_ms"],
            entry.get("chunk_count", 0),
            entry.get("response_bytes", 0),
        )

        with open(self._log_path(), "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    async def _watch_pending(self):
        """Periodically log requests that appear stuck or unusually long-lived,
        and refresh the run marker so a silent kill leaves a recent RSS reading.
        """
        import lifecycle

        while True:
            await asyncio.sleep(self._watchdog_interval_s)
            now = time.time()
            with self._lock:
                pending_items = list(self._pending.values())

            # Memory trend. A jetsam (out-of-memory) kill leaves no crash
            # report and no traceback, so the only evidence is RSS climbing
            # in the log right up to the moment the process vanishes.
            rss_mb = lifecycle.peak_rss_mb()
            if rss_mb - self._last_logged_rss_mb >= self._rss_log_step_mb:
                self._last_logged_rss_mb = rss_mb
                logger.warning(
                    "peak RSS now %.0f MB (%d request(s) in flight)",
                    rss_mb, len(pending_items),
                )
            lifecycle.heartbeat()

            for entry in pending_items:
                age_s = now - entry.get("started_at", now)
                since_activity_s = now - entry.get("last_activity_at", now)
                if age_s < self._stale_after_s:
                    continue
                if now - entry.get("watchdog_last_log_at", 0.0) < self._watchdog_interval_s:
                    continue

                entry["watchdog_last_log_at"] = now
                phase = "streaming" if entry.get("chunk_count", 0) > 0 else "waiting_for_first_byte"
                logger.warning(
                    "Request still in flight after %.1fs: %s %s phase=%s status=%s chunks=%d bytes=%d idle_for=%.1fs",
                    age_s,
                    entry.get("method", "?"),
                    entry.get("path", "?"),
                    phase,
                    entry.get("response_status", "pending"),
                    entry.get("chunk_count", 0),
                    entry.get("response_bytes", 0),
                    since_activity_s,
                )
