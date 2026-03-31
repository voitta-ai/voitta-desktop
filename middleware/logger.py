"""Request logger middleware — writes every request/response to JSONL."""

import json
import logging
import threading
import time
from pathlib import Path

from .base import Middleware, ProxyRequest, ProxyResponse, decompress

logger = logging.getLogger("voitta-desktop.logger")

LOG_DIR = Path.home() / ".voitta-desktop" / "logs"


class RequestLogger(Middleware):
    """Logs every request and response to a JSONL file, one file per day."""

    def __init__(
        self,
        log_dir: Path = LOG_DIR,
        stale_after_s: int = 60,
        watchdog_interval_s: int = 30,
    ):
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._pending: dict[int, dict] = {}
        self._stale_after_s = stale_after_s
        self._watchdog_interval_s = watchdog_interval_s
        self._lock = threading.Lock()
        self._watchdog = threading.Thread(target=self._watch_pending, daemon=True)
        self._watchdog.start()

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
            "request_body": request_body,
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
            events = []
            for line in response_text.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            entry["response_events"] = events
        else:
            entry["response_body"] = json.loads(response_text) if response_text else None

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

    def _watch_pending(self):
        """Periodically log requests that appear stuck or unusually long-lived."""
        while True:
            time.sleep(self._watchdog_interval_s)
            now = time.time()
            with self._lock:
                pending_items = list(self._pending.values())

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
