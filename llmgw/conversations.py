"""/voitta-store: keep a Claude Code conversation in Voitta Desktop's own folder.

The source is Claude Code's transcript (``~/.claude/projects/<project>/<session>.jsonl``):
append-only, so it holds the whole history, compacted turns included, with the
model, effort and timestamps of every answer, plus the window's subagent
transcripts beside it. Voitta adds what only it knows: which account each
request was routed to and who actually answered (the routing journal), and
the window's current LLM settings.

One file per window, ``<conversations>/<session id>.json``; storing again
replaces it with the newer, longer copy. The folder is outside the app bundle
and outside what the startup purge clears, so it survives restarts and updates.
"""

import collections
import json
import os
import time
from pathlib import Path

FORMAT = "voitta-conversation/1"


def claude_projects_dirs() -> list[Path]:
    dirs = [Path.home() / ".claude" / "projects"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "projects")
    return dirs


def find_transcript(session_id: str, hint: str | None, projects_dirs: list[Path]) -> Path | None:
    """The window's transcript. ``hint`` is the path Claude Code reported to the
    mod; it is used only if it really is that session's file inside a Claude
    projects folder, so the endpoint can't be pointed at anything else."""
    roots = [d.resolve() for d in projects_dirs if d.is_dir()]
    if hint:
        p = Path(hint).expanduser().resolve()
        if p.name == f"{session_id}.jsonl" and p.is_file() and any(p.parent.parent == r for r in roots):
            return p
    for root in roots:
        for p in root.glob(f"*/{session_id}.jsonl"):
            return p
    return None


def _read_jsonl(path: Path) -> tuple[list, int]:
    records, unreadable = [], 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                unreadable += 1  # a line Claude Code was still writing
    return records, unreadable


def _requested_models(records: list) -> collections.Counter:
    c = collections.Counter()
    for r in records:
        m = r.get("message") if r.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("model"):
            c[m["model"]] += 1
    return c


def build(session_id: str, transcript: Path, routing: list[dict], *, claude_code: dict, llm: dict) -> dict:
    records, unreadable = _read_jsonl(transcript)
    subagents = {}
    for p in sorted((transcript.parent / session_id / "subagents").glob("*.jsonl")):
        subagents[p.stem], bad = _read_jsonl(p)
        unreadable += bad
    requested = _requested_models(records)
    for recs in subagents.values():
        requested += _requested_models(recs)
    answered = collections.Counter()
    for r in routing:
        up = r.get("upstream") or {}
        # The provider's own report, else the model it was asked to run; for
        # "As is" the transcript already holds Anthropic's answer.
        model = up.get("model") or r.get("upstream_model")
        if model:
            answered[f"{model} @ {up.get('host')}"] += 1
        elif r.get("account_id") == "as-is":
            answered[f"(see transcript) @ {up.get('host')}"] += 1
    title = next((r.get("aiTitle") for r in reversed(records) if r.get("type") == "ai-title"), None)
    version = next((r.get("version") for r in reversed(records) if r.get("version")), None)
    cwd = next((r.get("cwd") for r in reversed(records) if r.get("cwd")), None)
    return {
        "format": FORMAT,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_id": session_id,
        "title": title,
        "cwd": claude_code.get("cwd") or cwd,
        "claude_code": {"version": version, **claude_code},
        "llm": llm,
        "models": {
            # What Claude Code asked for / recorded, per answer.
            "requested": dict(requested.most_common()),
            # What actually answered, per request Voitta routed (its journal).
            "answered_by": dict(answered.most_common()),
        },
        "counts": {
            "records": len(records),
            "user": sum(r.get("type") == "user" for r in records),
            "assistant": sum(r.get("type") == "assistant" for r in records),
            "subagents": len(subagents),
            "routed_requests": len(routing),
            "unreadable_lines": unreadable,
        },
        "source": str(transcript),
        "routing": routing,
        "transcript": records,
        "subagents": subagents,
    }


def write(folder: Path, record: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{record['session_id']}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path
