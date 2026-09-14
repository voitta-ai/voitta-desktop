"""Read-only access to Claude Code's on-disk session transcripts.

The desktop app never captures full message content itself (the tracker
keeps 80-char summaries; the request logger truncates) — but Claude Code
writes complete transcripts to::

    ~/.claude/projects/<project-slug>/<session-id>.jsonl            (main thread)
    ~/.claude/projects/<project-slug>/<session-id>/subagents/agent-<id>.jsonl

This module locates those files by session id (the slug is unknown, so we
glob), parses them into turn-grouped entries for the Session Explorer, and
matches sub-agent API threads back to their transcript files by first-message
identity.

The JSONL format is Claude Code's internal one — undocumented and free to
change between releases. Everything here is defensive: unknown entry types
are skipped, unparseable lines are dropped, and any failure degrades to an
empty result rather than an exception reaching the UI.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

from .models import parse_image_dimensions

logger = logging.getLogger("voitta-desktop.transcripts")

# Entry types rendered by the Explorer. Everything else in the file
# (ai-title, attachment, file-history-snapshot, queue-operation, mode,
# last-prompt, ...) is bookkeeping, not conversation.
_RENDERED_TYPES = {"user", "assistant"}

# Per-block text cap pushed to the webview. Full length is always reported
# in ``chars``; the raw entry is available on demand via raw_entry().
_TEXT_CAP = 16_000
# Cap on serialized tool_use input.
_INPUT_CAP = 4_000
# Compare this many normalized chars when matching an API thread's first
# message against a sub-agent transcript's first user message.
_SEED_MATCH_LEN = 400

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _norm(text: str) -> str:
    """Whitespace-insensitive normalization for seed matching."""
    return re.sub(r"\s+", " ", text).strip()


def _entry_text(message: dict) -> str:
    """Join the plain-text blocks of a transcript message's content."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class TranscriptStore:
    """Locates and parses Claude Code transcripts, with light caching.

    All methods are safe to call from any thread; state is a handful of
    dict caches guarded by the GIL (worst case a cache entry is computed
    twice). File reads are small or line-streamed — no whole-file loads
    except for parse(), which the UI only requests for the selected
    conversation.
    """

    def __init__(self, projects_dir: Path = _PROJECTS_DIR):
        self._projects_dir = projects_dir
        # sid -> main transcript path (or None when known-absent; retried
        # after _MISS_TTL so a transcript that appears later is found).
        self._main_path: dict[str, tuple[float, Path | None]] = {}
        # sid -> (subagents dir mtime, {norm seed prefix -> agent_id})
        self._agent_seeds: dict[str, tuple[float, dict[str, str]]] = {}
        # (path, uuid, seq) -> thumbnail b64; bounded, see _thumb().
        self._thumbs: dict[tuple[str, str, int], str] = {}

    _MISS_TTL = 10.0  # seconds before re-globbing for a missing transcript

    # ── Location ─────────────────────────────────────────────────────────────

    def find_main(self, sid: str) -> Path | None:
        """Main-thread transcript for a session id, or None."""
        if not sid or "/" in sid or sid.startswith("anon-"):
            return None
        cached = self._main_path.get(sid)
        if cached is not None:
            ts, path = cached
            if path is not None and path.exists():
                return path
            if path is None and time.time() - ts < self._MISS_TTL:
                return None
        path = None
        try:
            for hit in self._projects_dir.glob(f"*/{sid}.jsonl"):
                path = hit
                break
        except OSError:
            pass
        self._main_path[sid] = (time.time(), path)
        return path

    def _subagents_dir(self, sid: str) -> Path | None:
        main = self.find_main(sid)
        if main is None:
            return None
        d = main.parent / sid / "subagents"
        return d if d.is_dir() else None

    # ── Sub-agent matching ───────────────────────────────────────────────────

    def _first_user_text(self, path: Path) -> str:
        """First user entry's plain text, reading as little as possible."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _ in range(50):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") != "user":
                        continue
                    msg = entry.get("message") or {}
                    text = _entry_text(msg)
                    if text:
                        return text
        except OSError:
            pass
        return ""

    def _agent_seed_map(self, sid: str) -> dict[str, str]:
        """{normalized-first-message-prefix: agent_id} for a session."""
        d = self._subagents_dir(sid)
        if d is None:
            return {}
        try:
            mtime = d.stat().st_mtime
        except OSError:
            return {}
        cached = self._agent_seeds.get(sid)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        seeds: dict[str, str] = {}
        try:
            files = sorted(d.glob("agent-*.jsonl"))
        except OSError:
            files = []
        for f in files:
            agent_id = f.stem[len("agent-"):]
            text = _norm(self._first_user_text(f))
            if text:
                seeds[text[:_SEED_MATCH_LEN]] = agent_id
        self._agent_seeds[sid] = (mtime, seeds)
        return seeds

    def find_agent_by_seed(self, sid: str, seed: str) -> str | None:
        """Match an API thread's first user message to a sub-agent transcript.

        The sub-agent's first API request carries the Task prompt as its
        first message, and the transcript records the same prompt — so a
        normalized prefix comparison identifies the agent exactly.
        """
        if not seed:
            return None
        key = _norm(seed)[:_SEED_MATCH_LEN]
        if not key:
            return None
        return self._agent_seed_map(sid).get(key)

    def agent_path(self, sid: str, agent_id: str) -> Path | None:
        d = self._subagents_dir(sid)
        if d is None or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id or ""):
            return None
        p = d / f"agent-{agent_id}.jsonl"
        return p if p.exists() else None

    def list_agents(self, sid: str) -> list[dict]:
        """All sub-agent transcripts for a session: id + label, cheap."""
        d = self._subagents_dir(sid)
        if d is None:
            return []
        out = []
        try:
            files = sorted(d.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return []
        for f in files:
            agent_id = f.stem[len("agent-"):]
            label = _norm(self._first_user_text(f))[:60] or agent_id
            out.append({"agent_id": agent_id, "label": label})
        return out

    # ── Parsing ──────────────────────────────────────────────────────────────

    def resolve_path(self, conv_id: str) -> Path | None:
        """Transcript path for a tracker conversation id.

        Main conversations use their session id; child (sub-agent)
        conversations are ``<sid>#<hash>`` and resolve through their
        recorded agent id — the caller passes that via ``agent_ref``
        instead, so here we only split the forms we can.
        """
        if "#" in conv_id:
            return None  # needs agent_id — use agent_path()
        return self.find_main(conv_id)

    def parse(self, path: Path) -> dict:
        """Parse a transcript file into turn-grouped renderable entries.

        Returns ``{"turns": [...], "cursor": <file size>, "entry_count": n}``.
        Never raises: unreadable file → empty result.
        """
        entries = []
        size = 0
        try:
            size = path.stat().st_size
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError as e:
            logger.warning("transcript read failed: %s: %s", path, e)
            return {"turns": [], "cursor": 0, "entry_count": 0}

        chain = self._active_chain(entries)
        turns = self._group_turns(chain, path)
        return {"turns": turns, "cursor": size, "entry_count": len(chain)}

    def _active_chain(self, entries: list[dict]) -> list[dict]:
        """Drop abandoned branches: keep only entries on the parent chain of
        the newest message, in file order. Compact boundaries are kept as
        markers wherever they appear."""
        by_uuid = {e.get("uuid"): e for e in entries if e.get("uuid")}
        leaf = None
        for e in reversed(entries):
            if e.get("type") in _RENDERED_TYPES:
                leaf = e
                break
        if leaf is None:
            return [e for e in entries if self._is_compact_boundary(e)]

        keep: set[str] = set()
        node, hops = leaf, 0
        while node is not None and hops < 100_000:
            uid = node.get("uuid")
            if not uid or uid in keep:
                break
            keep.add(uid)
            node = by_uuid.get(node.get("parentUuid"))
            hops += 1

        chain = []
        for e in entries:
            if self._is_compact_boundary(e):
                chain.append(e)
            elif e.get("type") in _RENDERED_TYPES and e.get("uuid") in keep:
                chain.append(e)
        return chain

    @staticmethod
    def _is_compact_boundary(e: dict) -> bool:
        return e.get("type") == "system" and e.get("subtype") == "compact_boundary"

    def _group_turns(self, chain: list[dict], path: Path) -> list[dict]:
        """Group chain entries into user→assistant turns.

        A new turn starts at a user entry that carries actual user input
        (no tool_result blocks) once the current turn has assistant
        output — the same boundary rule the tracker uses on API bodies.
        """
        turns: list[dict] = []
        cur: dict | None = None

        def close():
            nonlocal cur
            if cur is not None and cur["entries"]:
                turns.append(cur)
            cur = None

        def fresh() -> dict:
            return {
                "index": len(turns), "compact": False, "entries": [],
                "usage": None, "ts_start": None, "ts_end": None,
            }

        for e in chain:
            if self._is_compact_boundary(e):
                close()
                turns.append({
                    "index": len(turns), "compact": True, "entries": [],
                    "usage": None,
                    "ts_start": e.get("timestamp"), "ts_end": e.get("timestamp"),
                })
                continue

            etype = e.get("type")
            msg = e.get("message") or {}
            content = msg.get("content", "")

            if etype == "user":
                has_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                saw_assistant = cur is not None and any(
                    en["role"] == "assistant" for en in cur["entries"]
                )
                if not has_tool_result and saw_assistant:
                    close()

            if cur is None:
                cur = fresh()

            rendered = self._render_entry(e, path)
            if rendered is None:
                continue
            cur["entries"].append(rendered)
            ts = e.get("timestamp")
            if ts:
                cur["ts_start"] = cur["ts_start"] or ts
                cur["ts_end"] = ts
            if etype == "assistant":
                usage = msg.get("usage") or {}
                if usage:
                    prev_out = (cur["usage"] or {}).get("output_tokens", 0)
                    cur["usage"] = {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": prev_out + usage.get("output_tokens", 0),
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    }

        close()
        return turns

    def _render_entry(self, e: dict, path: Path) -> dict | None:
        msg = e.get("message") or {}
        role = msg.get("role") or e.get("type")
        blocks = self._render_blocks(msg, e, path)
        if not blocks:
            return None
        out = {
            "uuid": e.get("uuid", ""),
            "role": role,
            "ts": e.get("timestamp", ""),
            "meta": bool(e.get("isMeta")),
            "blocks": blocks,
        }
        model = msg.get("model")
        if model:
            out["model"] = model
        return out

    def _render_blocks(self, msg: dict, entry: dict, path: Path) -> list[dict]:
        content = msg.get("content", "")
        role = msg.get("role", "")
        blocks: list[dict] = []

        if isinstance(content, str):
            if content.strip():
                blocks.append(self._text_block(content, role))
            return blocks
        if not isinstance(content, list):
            return blocks

        img_seq = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            btype = item.get("type", "")

            if btype == "text":
                text = item.get("text", "")
                if text.strip():
                    blocks.append(self._text_block(text, role))

            elif btype == "thinking":
                text = item.get("thinking", "")
                blocks.append({
                    "t": "thinking",
                    "text": text[:_TEXT_CAP],
                    "chars": len(text),
                })

            elif btype == "tool_use":
                inp = item.get("input", {})
                inp_json = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
                preview = ""
                if isinstance(inp, dict):
                    for key in ("command", "query", "file_path", "pattern",
                                "description", "prompt", "url"):
                        if key in inp:
                            preview = str(inp[key])[:120]
                            break
                b = {
                    "t": "tool_use",
                    "id": item.get("id", ""),
                    "name": item.get("name", "tool"),
                    "preview": preview,
                    "input": inp_json[:_INPUT_CAP],
                    "chars": len(inp_json),
                }
                if item.get("name") == "Task" and isinstance(inp, dict):
                    b["task"] = {
                        "description": str(inp.get("description", ""))[:120],
                        "subagent_type": str(inp.get("subagent_type", ""))[:60],
                        "prompt_seed": _norm(str(inp.get("prompt", "")))[:_SEED_MATCH_LEN],
                    }
                blocks.append(b)

            elif btype == "tool_result":
                rc = item.get("content", "")
                text = ""
                images = []
                if isinstance(rc, str):
                    text = rc
                elif isinstance(rc, list):
                    parts = []
                    for sub in rc:
                        if not isinstance(sub, dict):
                            continue
                        if sub.get("type") == "text":
                            parts.append(sub.get("text", ""))
                        elif sub.get("type") == "image":
                            images.append(self._image_block(sub, entry, path, img_seq))
                            img_seq += 1
                    text = "\n".join(parts)
                blocks.append({
                    "t": "tool_result",
                    "tool_use_id": item.get("tool_use_id", ""),
                    "error": bool(item.get("is_error")),
                    "text": text[:_TEXT_CAP],
                    "chars": len(text),
                    "images": images,
                })

            elif btype == "image":
                blocks.append(self._image_block(item, entry, path, img_seq))
                img_seq += 1

        return blocks

    @staticmethod
    def _text_block(text: str, role: str) -> dict:
        return {
            "t": "text",
            "role": role,
            "text": text[:_TEXT_CAP],
            "chars": len(text),
        }

    # ── Images ───────────────────────────────────────────────────────────────

    def _image_block(self, item: dict, entry: dict, path: Path, seq: int) -> dict:
        """Image metadata + thumbnail; full data stays out of the payload."""
        src = item.get("source", {}) if isinstance(item, dict) else {}
        media_type = src.get("media_type", "image/png")
        data = src.get("data", "") if src.get("type") == "base64" else ""
        raw_bytes = (len(data) * 3) // 4
        w = h = 0
        thumb = ""
        if data:
            try:
                head = base64.b64decode(data[:174_763] + "=" * ((4 - len(data) % 4) % 4))
                w, h = parse_image_dimensions(head, media_type)
            except Exception:
                pass
            thumb = self._thumb(path, entry.get("uuid", ""), seq, data)
        return {
            "t": "image", "media_type": media_type, "w": w, "h": h,
            "bytes": raw_bytes, "thumb": thumb, "seq": seq,
        }

    def _thumb(self, path: Path, uuid: str, seq: int, data: str) -> str:
        key = (str(path), uuid, seq)
        hit = self._thumbs.get(key)
        if hit is not None:
            return hit
        thumb = ""
        try:
            import io
            from PIL import Image as PILImage
            raw = base64.b64decode(data + "=" * ((4 - len(data) % 4) % 4))
            img = PILImage.open(io.BytesIO(raw))
            tw = min(200, img.width)
            th = max(1, int(img.height * tw / max(img.width, 1)))
            img = img.convert("RGB").resize((tw, th))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            thumb = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            thumb = ""
        if len(self._thumbs) > 300:
            self._thumbs.clear()
        self._thumbs[key] = thumb
        return thumb

    def get_image(self, path: Path, uuid: str, seq: int) -> tuple[str, str] | None:
        """Full-size base64 for one image, fetched on click only."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if uuid not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("uuid") != uuid:
                        continue
                    msg = entry.get("message") or {}
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        return None
                    # Same traversal order as _render_blocks assigns seq.
                    ordered = []
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_result":
                            rc = item.get("content", [])
                            if isinstance(rc, list):
                                ordered.extend(
                                    s for s in rc
                                    if isinstance(s, dict) and s.get("type") == "image"
                                )
                        elif item.get("type") == "image":
                            ordered.append(item)
                    if 0 <= seq < len(ordered):
                        src = ordered[seq].get("source", {})
                        return (src.get("media_type", "image/png"),
                                src.get("data", ""))
                    return None
        except OSError:
            pass
        return None

    # ── Raw entries ──────────────────────────────────────────────────────────

    def raw_entry(self, path: Path, uuid: str, cap: int = 200_000) -> str:
        """Pretty-printed raw JSON for one entry, size-capped."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if uuid not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("uuid") == uuid:
                        text = json.dumps(entry, indent=2, ensure_ascii=False)
                        if len(text) > cap:
                            text = text[:cap] + f"\n…[+{len(text) - cap} chars]"
                        return text
        except OSError:
            pass
        return "(entry not found)"
