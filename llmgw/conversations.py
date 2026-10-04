"""Stored conversations: /voitta-store's copies, and everything the window does with them.

The source is Claude Code's transcript (``~/.claude/projects/<project>/<session>.jsonl``):
append-only, so it holds the whole history, compacted turns included, with the
model, effort and timestamps of every answer, plus the window's subagent
transcripts beside it. Voitta adds what only it knows: which account each
request was routed to and who actually answered (the routing journal), and
the window's current LLM settings.

One folder per window::

    <conversations>/<session id>/
        meta.json            what the list and the header show (small)
        transcript.jsonl     Claude Code's transcript, byte for byte
        subagents/…          its subagent transcripts, byte for byte
        routing.jsonl        Voitta's routing journal for the window

Storing again replaces the folder with the newer, longer copy. The folder is
outside the app bundle and outside what the startup purge clears, so it
survives restarts and updates. Nothing here touches Claude Code's own files.
"""

import collections
import datetime
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from middleware.transcripts import active_chain

log = logging.getLogger("voitta-desktop.llm")

FORMAT = "voitta-conversation/1"          # the one-file JSON export
FOLDER_FORMAT = "voitta-conversation-folder/1"
SAFE_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")

# Called (no arguments) after a conversation is stored or deleted; the menu
# bar keeps its count from this. Listeners must not block.
_listeners: list = []
_lock = threading.Lock()


def on_change(callback):
    _listeners.append(callback)


def _changed():
    for cb in list(_listeners):
        try:
            cb()
        except Exception:
            log.exception("conversation listener failed")


# ---- finding Claude Code's transcript ------------------------------------------

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


# ---- reading ------------------------------------------------------------------

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


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _first_prompt(records: list) -> str:
    for r in records:
        if r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
            text = _text_of((r.get("message") or {}).get("content")).strip()
            if text and not text.startswith("<"):
                return text[:300]
    return ""


def _requested_models(records: list) -> collections.Counter:
    c = collections.Counter()
    for r in records:
        m = r.get("message") if r.get("type") == "assistant" else None
        if isinstance(m, dict) and m.get("model"):
            c[m["model"]] += 1
    return c


def _answered_by(routing: list[dict]) -> collections.Counter:
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
    return answered


def _meta(session_id: str, records: list, subagents: dict[str, list], routing: list[dict], unreadable: int,
          *, claude_code: dict, llm: dict, source: str) -> dict:
    requested = _requested_models(records)
    for recs in subagents.values():
        requested += _requested_models(recs)
    title = next((r.get("aiTitle") for r in reversed(records) if r.get("type") == "ai-title"), None)
    version = next((r.get("version") for r in reversed(records) if r.get("version")), None)
    cwd = claude_code.get("cwd") or next((r.get("cwd") for r in reversed(records) if r.get("cwd")), None)
    stamps = [r["timestamp"] for r in records if isinstance(r.get("timestamp"), str)]
    return {
        "format": FOLDER_FORMAT,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stored_ts": time.time(),
        "session_id": session_id,
        "title": title,
        "first_prompt": _first_prompt(records),
        "cwd": cwd,
        "project": Path(cwd).name if cwd else None,
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
        "claude_code": {"version": version, **claude_code},
        "llm": llm,
        "models": {
            # What Claude Code asked for / recorded, per answer.
            "requested": dict(requested.most_common()),
            # What actually answered, per request Voitta routed (its journal).
            "answered_by": dict(_answered_by(routing).most_common()),
        },
        "counts": {
            "records": len(records),
            "user": sum(r.get("type") == "user" for r in records),
            "assistant": sum(r.get("type") == "assistant" for r in records),
            "subagents": len(subagents),
            "routed_requests": len(routing),
            "unreadable_lines": unreadable,
        },
        "subagents": [{"id": k, "label": _first_prompt(v)[:120] or k} for k, v in subagents.items()],
        "source": source,
    }


# ---- storing --------------------------------------------------------------------

def _private_write(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _private_copy(src: Path, dst: Path):
    with src.open("rb") as f:
        _private_write(dst, f.read())


def _dir_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def _swap_in(folder: Path, session_id: str, tmp: Path) -> Path:
    """Replace ``<folder>/<session_id>`` with ``tmp`` (a fully written copy)."""
    final = folder / session_id
    old = folder / f".old-{session_id}-{uuid.uuid4().hex[:8]}"
    with _lock:
        if final.exists():
            final.rename(old)
        tmp.rename(final)
    shutil.rmtree(old, ignore_errors=True)
    return final


def store(folder: Path, session_id: str, transcript: Path, routing: list[dict], *,
          claude_code: dict, llm: dict) -> dict:
    """Copy one window's conversation into ``folder``; returns its meta."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = folder / f".tmp-{session_id}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir(mode=0o700)
    try:
        _private_copy(transcript, tmp / "transcript.jsonl")
        records, unreadable = _read_jsonl(tmp / "transcript.jsonl")
        subagents = {}
        src_agents = transcript.parent / session_id / "subagents"
        if src_agents.is_dir():
            (tmp / "subagents").mkdir(mode=0o700)
            for p in sorted(src_agents.iterdir()):
                if p.is_file():
                    _private_copy(p, tmp / "subagents" / p.name)
                    if p.suffix == ".jsonl":
                        subagents[p.stem], bad = _read_jsonl(tmp / "subagents" / p.name)
                        unreadable += bad
        _private_write(tmp / "routing.jsonl",
                       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in routing).encode())
        meta = _meta(session_id, records, subagents, routing, unreadable,
                     claude_code=claude_code, llm=llm, source=str(transcript))
        meta["bytes"] = _dir_bytes(tmp)
        _private_write(tmp / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode())
        _swap_in(folder, session_id, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    (folder / f"{session_id}.json").unlink(missing_ok=True)  # a first-version copy this one supersedes
    _changed()
    return meta


def _migrate_single_files(folder: Path):
    """The first /voitta-store build wrote one ``<session>.json`` per window;
    turn those into folders (the transcript is re-serialized, as that file
    no longer holds the original bytes)."""
    for old in folder.glob("*.json"):
        sid = old.stem
        if not SAFE_ID.fullmatch(sid):
            continue
        if (folder / sid).exists():  # stored again since: the folder is newer
            old.unlink(missing_ok=True)
            continue
        try:
            doc = json.loads(old.read_text(encoding="utf-8"))
            tmp = folder / f".tmp-{sid}-{uuid.uuid4().hex[:8]}"
            tmp.mkdir(mode=0o700)
            lines = lambda recs: "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode()
            _private_write(tmp / "transcript.jsonl", lines(doc.get("transcript") or []))
            if doc.get("subagents"):
                (tmp / "subagents").mkdir(mode=0o700)
                for agent, recs in doc["subagents"].items():
                    _private_write(tmp / "subagents" / f"{agent}.jsonl", lines(recs))
            _private_write(tmp / "routing.jsonl", lines(doc.get("routing") or []))
            meta = _meta(sid, doc.get("transcript") or [], doc.get("subagents") or {}, doc.get("routing") or [],
                         (doc.get("counts") or {}).get("unreadable_lines", 0),
                         claude_code=doc.get("claude_code") or {}, llm=doc.get("llm") or {},
                         source=doc.get("source") or "")
            meta["stored_at"] = doc.get("stored_at") or meta["stored_at"]
            stamp = _parse_ts(meta["stored_at"])
            meta["stored_ts"] = stamp if stamp is not None else old.stat().st_mtime
            meta["bytes"] = _dir_bytes(tmp)
            _private_write(tmp / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode())
            _swap_in(folder, sid, tmp)
            old.unlink()
            log.info("moved stored conversation %s to the folder layout", sid[:8])
        except Exception:
            log.exception("could not migrate %s", old)


# ---- the window's view ------------------------------------------------------------

def conversation_dir(folder: Path, session_id: str) -> Path:
    if not SAFE_ID.fullmatch(session_id or ""):
        raise ValueError(f"not a session id: {session_id!r}")
    d = folder / session_id
    if not (d / "meta.json").is_file():
        raise FileNotFoundError(f"no stored conversation {session_id}")
    return d


def list_conversations(folder: Path) -> list[dict]:
    """Every stored conversation's meta, newest first. Reads only meta.json."""
    if not folder.is_dir():
        return []
    _migrate_single_files(folder)
    out = []
    for d in folder.iterdir():
        if d.name.startswith(".") or not (d / "meta.json").is_file():
            continue
        try:
            out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            log.warning("unreadable stored conversation %s", d)
    out.sort(key=lambda m: -(m.get("stored_ts") or 0))
    return out


def count(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for d in folder.iterdir() if not d.name.startswith(".") and (d / "meta.json").is_file()) \
        + sum(1 for p in folder.glob("*.json") if SAFE_ID.fullmatch(p.stem) and not (folder / p.stem).exists())


def load_meta(folder: Path, session_id: str) -> dict:
    return json.loads((conversation_dir(folder, session_id) / "meta.json").read_text(encoding="utf-8"))


def transcript_path(folder: Path, session_id: str, agent: str | None = None) -> Path:
    d = conversation_dir(folder, session_id)
    if not agent:
        return d / "transcript.jsonl"
    if not _AGENT_ID.fullmatch(agent):
        raise ValueError(f"not a subagent id: {agent!r}")
    p = d / "subagents" / f"{agent}.jsonl"
    if not p.is_file():
        raise FileNotFoundError(f"no subagent {agent}")
    return p


def routing(folder: Path, session_id: str) -> list[dict]:
    p = conversation_dir(folder, session_id) / "routing.jsonl"
    return _read_jsonl(p)[0] if p.is_file() else []


def delete(folder: Path, session_ids: list[str]) -> int:
    gone = 0
    for sid in session_ids:
        try:
            d = conversation_dir(folder, sid)
        except (ValueError, FileNotFoundError):
            continue
        shutil.rmtree(d)
        gone += 1
    if gone:
        _changed()
    return gone


# ---- which request answered which message -------------------------------------------

def _parse_ts(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None


def route_for(entry_ts, entry_model: str | None, journal: list[dict], slack_s: float = 3.0) -> dict | None:
    """The journal record that produced an assistant message, when exactly one
    fits: same requested model, and the message written while that request
    was open. Two candidates (parallel requests) means no answer, not a guess."""
    t = _parse_ts(entry_ts)
    if t is None or not entry_model:
        return None
    hits = [r for r in journal
            if r.get("model") == entry_model and r.get("status") == 200 and isinstance(r.get("ts"), (int, float))
            and r["ts"] - 1.0 <= t <= r["ts"] + (r.get("ms") or 0) / 1000 + slack_s]
    return hits[0] if len(hits) == 1 else None


def route_label(r: dict) -> str:
    up = r.get("upstream") or {}
    model = up.get("model") or r.get("upstream_model")
    who = f"{model} @ {up.get('host')}" if model else f"As is @ {up.get('host')}"
    return f"{who} · {r.get('account')} · {'window pick' if r.get('route') == 'window' else 'default'}"


def annotate_turns(turns: list[dict], journal: list[dict]):
    """Mark assistant entries of a parsed transcript with who answered them."""
    if not journal:
        return
    for turn in turns:
        for e in turn.get("entries") or []:
            if e.get("role") == "assistant":
                r = route_for(e.get("ts"), e.get("model"), journal)
                if r:
                    e["route"] = route_label(r)


# ---- exports --------------------------------------------------------------------------

EXPORTS = {"md": ".md", "json": ".json", "jsonl": ".jsonl"}


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _details(summary: str, body: str) -> str:
    return f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>"


def _blocks_md(msg: dict, role: str) -> list[str]:
    content = msg.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            out.append(b["text"])
        elif t == "thinking" and b.get("thinking"):
            out.append(_details(f"💭 thinking ({len(b['thinking']):,} chars)", _fence(b["thinking"])))
        elif t == "redacted_thinking":
            out.append("_(redacted thinking)_")
        elif t == "tool_use":
            inp = b.get("input") or {}
            hint = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            summary = f"🔧 {b.get('name')}" + (f" — {str(hint)[:120]}" if hint else "")
            out.append(_details(summary.replace("<", "&lt;"),
                                _fence(json.dumps(inp, indent=2, ensure_ascii=False), "json")))
        elif t == "tool_result":
            body = b.get("content")
            text = body if isinstance(body, str) else "\n".join(
                x.get("text", "") if x.get("type") == "text" else f"[{x.get('type')}]"
                for x in (body or []) if isinstance(x, dict))
            label = "↳ result" + (" (error)" if b.get("is_error") else "") + f" ({len(text):,} chars)"
            out.append(_details(label, _fence(text)))
        elif t == "image":
            src = b.get("source") or {}
            out.append(f"_[image: {src.get('media_type', '?')}, {len(src.get('data') or '') * 3 // 4:,} bytes]_")
        elif t == "document":
            out.append(f"_[document: {(b.get('source') or {}).get('media_type', '?')}]_")
    return out


def _transcript_md(records: list[dict], journal: list[dict]) -> str:
    parts, turn = [], 0
    for r in active_chain(records, through_compactions=True):
        if r.get("type") == "system":
            parts.append(f"\n> ✂ **Context compacted here** ({r.get('timestamp', '')}) — "
                         "the turns above were summarized for the model; they are kept here in full.\n")
            continue
        msg = r.get("message") or {}
        blocks = _blocks_md(msg, r.get("type"))
        if not blocks:
            continue
        ts = r.get("timestamp", "")
        if r.get("type") == "user":
            is_input = not any(isinstance(b, dict) and b.get("type") == "tool_result"
                               for b in (msg.get("content") if isinstance(msg.get("content"), list) else []))
            if r.get("isCompactSummary"):
                parts.append(_details("📋 compaction summary given to the model", "\n\n".join(blocks)))
            elif r.get("isMeta"):
                parts.append(_details("ⓘ meta message", "\n\n".join(blocks)))
            elif is_input:
                turn += 1
                parts.append(f"\n---\n\n## Turn {turn} · {ts}\n\n**You**\n\n" + "\n\n".join(blocks))
            else:
                parts.append("\n\n".join(blocks))
        else:
            head = f"**Claude** · {msg.get('model') or '?'}"
            route = route_for(ts, msg.get("model"), journal)
            if route:
                head += f" · answered by {route_label(route)}"
            parts.append(f"{head}\n\n" + "\n\n".join(blocks))
    return "\n\n".join(parts)


def to_markdown(folder: Path, session_id: str) -> str:
    meta = load_meta(folder, session_id)
    journal = routing(folder, session_id)
    records = _read_jsonl(transcript_path(folder, session_id))[0]
    acct = (meta.get("llm") or {}).get("account") or {}
    fmt_counts = lambda d: ", ".join(f"{k} ×{v}" for k, v in (d or {}).items()) or "—"
    lines = [
        f"# {meta.get('title') or meta.get('first_prompt', '')[:80] or session_id}",
        "",
        f"- Session: `{session_id}`",
        f"- Project: `{meta.get('cwd') or '?'}`",
        f"- Stored: {meta.get('stored_at')} · Claude Code {(meta.get('claude_code') or {}).get('version') or '?'}",
        f"- Models requested: {fmt_counts((meta.get('models') or {}).get('requested'))}",
        f"- Answered by (routed requests): {fmt_counts((meta.get('models') or {}).get('answered_by'))}",
        f"- Window's LLM: {acct.get('label') or 'As is'} ({(meta.get('llm') or {}).get('route') or 'default'})",
        "",
        _transcript_md(records, journal),
    ]
    for agent in meta.get("subagents") or []:
        recs = _read_jsonl(transcript_path(folder, session_id, agent["id"]))[0]
        lines += ["", f"\n---\n\n# Subagent: {agent['label']}", f"`{agent['id']}`", "",
                  _transcript_md(recs, journal)]
    return "\n".join(lines) + "\n"


def to_json(folder: Path, session_id: str) -> bytes:
    """Everything in one document (the original /voitta-store format)."""
    meta = load_meta(folder, session_id)
    d = conversation_dir(folder, session_id)
    doc = {**{k: v for k, v in meta.items() if k not in ("format", "bytes")}, "format": FORMAT,
           "routing": routing(folder, session_id),
           "transcript": _read_jsonl(d / "transcript.jsonl")[0],
           "subagents": {a["id"]: _read_jsonl(transcript_path(folder, session_id, a["id"]))[0]
                         for a in meta.get("subagents") or []}}
    return json.dumps(doc, ensure_ascii=False).encode()


def _export_bytes(folder: Path, session_id: str, fmt: str) -> bytes:
    if fmt == "md":
        return to_markdown(folder, session_id).encode()
    if fmt == "json":
        return to_json(folder, session_id)
    if fmt == "jsonl":
        return transcript_path(folder, session_id).read_bytes()
    raise ValueError(f"unknown export format {fmt!r}")


def export_name(meta: dict, fmt: str) -> str:
    title = meta.get("title") or meta.get("first_prompt") or "conversation"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:60] or "conversation"
    return f"{slug}-{meta['session_id'][:8]}{EXPORTS[fmt]}"


def export(folder: Path, session_ids: list[str], fmt: str, dest: Path) -> Path:
    """One conversation → one file; several → a zip with one file each."""
    if fmt not in EXPORTS:
        raise ValueError(f"unknown export format {fmt!r}")
    if len(session_ids) == 1:
        data = _export_bytes(folder, session_ids[0], fmt)
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return dest
    tmp = dest.with_name(f".{dest.name}.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for sid in session_ids:
            z.writestr(export_name(load_meta(folder, sid), fmt), _export_bytes(folder, sid, fmt))
    os.replace(tmp, dest)
    return dest
