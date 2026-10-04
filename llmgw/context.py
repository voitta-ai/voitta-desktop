"""Record each window's system prompt and tool definitions.

Claude Code rebuilds both for every request and never writes them to its
transcript, so a stored conversation would otherwise lack them. The
conversation tracker hands over what Claude Code sent (before the optimizers
run); each distinct version is kept once per window:

    <folder>/<session id>/
        system-<hash>.json   the request's "system" field, as sent
        tools-<hash>.json    the request's "tools" field, as sent
        index.jsonl          one line per new (agent, system, tools) combination

Recording never touches the request: the tracker calls ``submit`` from the
event loop, the work runs on a worker thread, and any failure is logged and
dropped. /voitta-store copies the window's folder into the stored copy.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("voitta-desktop.llm")

_SAFE_ID = re.compile(r"[0-9A-Za-z-]{8,64}")
# Claude Code's billing header line changes on every request; versions differ
# only when the prompt itself does.
_CCH = re.compile(r"cch=[0-9a-f]+")
SESSION_HEADER = "x-claude-code-session-id"
AGENT_HEADER = "x-claude-code-agent-id"


def _digest(value) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(_CCH.sub("cch=0", text).encode()).hexdigest()[:16]


def _header(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name), None)


class ContextRecorder:
    KEEP_DAYS = 30

    def __init__(self, folder: Path):
        self.folder = folder
        self._seen: dict[str, set] = {}
        self._lock = threading.Lock()

    def dir_for(self, session_id: str) -> Path:
        return self.folder / session_id

    def submit(self, loop, headers: dict, system, tools, model: str | None):
        """From the event loop: record off-thread, never raise."""
        try:
            session_id = _header(headers, SESSION_HEADER)
            if not session_id or not _SAFE_ID.fullmatch(session_id) or (system is None and not tools):
                return
            agent = _header(headers, AGENT_HEADER)
            fut = loop.run_in_executor(None, self.record, session_id, agent, system, tools, model)
            fut.add_done_callback(_log_failure)
        except Exception:
            log.debug("context recording not scheduled", exc_info=True)

    def record(self, session_id: str, agent: str | None, system, tools, model: str | None):
        sys_h = _digest(system) if system is not None else None
        tools_h = _digest(tools) if tools else None
        key = (agent, sys_h, tools_h)
        with self._lock:
            seen = self._seen.get(session_id)
            if seen is None:
                seen = self._seen[session_id] = self._load_seen(session_id)
            if key in seen:
                return
            d = self.dir_for(session_id)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, value, h in (("system", system, sys_h), ("tools", tools, tools_h)):
                if h and not (d / f"{name}-{h}.json").exists():
                    _private_write(d / f"{name}-{h}.json", json.dumps(value, ensure_ascii=False).encode())
            line = {"ts": time.time(), "agent": agent, "model": model, "system": sys_h, "tools": tools_h,
                    "system_chars": len(json.dumps(system, ensure_ascii=False)) if system is not None else 0,
                    "tools_count": len(tools) if isinstance(tools, list) else 0}
            fd = os.open(d / "index.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
            seen.add(key)

    def _load_seen(self, session_id: str) -> set:
        return {(r.get("agent"), r.get("system"), r.get("tools")) for r in read_index(self.dir_for(session_id))}

    def prune(self):
        cutoff = time.time() - self.KEEP_DAYS * 86400
        if not self.folder.is_dir():
            return
        for d in self.folder.iterdir():
            try:
                index = d / "index.jsonl"
                if d.is_dir() and (not index.exists() or index.stat().st_mtime < cutoff):
                    shutil.rmtree(d)
            except OSError:
                pass


def _log_failure(fut):
    if not fut.cancelled() and fut.exception() is not None:
        log.warning("context recording failed: %s", fut.exception())


def _private_write(path: Path, data: bytes):
    tmp = path.with_name(f".{path.name}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---- reading (a recorder folder, or its copy inside a stored conversation) ----------

def read_index(folder: Path) -> list[dict]:
    try:
        lines = (folder / "index.jsonl").read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def load_part(folder: Path, name: str, digest: str | None):
    if not digest or not re.fullmatch(r"[0-9a-f]{16}", digest):
        return None
    try:
        return json.loads((folder / f"{name}-{digest}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
