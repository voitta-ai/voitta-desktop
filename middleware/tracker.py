"""Conversation tracker middleware — maintains per-session turn history."""

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from .base import Middleware, ProxyRequest, ProxyResponse, decompress, parse_int_param
from .models import Conversation, Turn
from .parsing import (
    parse_turns, compute_breakdown, extract_label, parse_sse_blocks,
)
from .transcripts import TranscriptStore

logger = logging.getLogger("voitta-desktop.tracker")

# The first user message after /compact starts with this fixed phrase —
# it marks a main-thread continuation, not a new (sub-agent) thread.
_COMPACT_PREFIX = "This session is being continued from a previous conversation"


class ConversationTracker(Middleware):
    """Tracks conversations with detailed content block history.

    One Claude Code session can carry several independent API threads: the
    main conversation plus one per Task sub-agent, all (potentially) sharing
    the same X-Claude-Code-Session-Id header. Each thread re-sends its own
    full history every call, so its first user message is a stable identity.
    Threads are therefore keyed by (session, first-message hash); collapsing
    them onto the session id alone made whichever thread answered last
    overwrite the others' turns.
    """

    def __init__(self, transcripts: TranscriptStore | None = None):
        self.conversations: dict[str, Conversation] = {}
        self._pending: dict[int, dict] = {}
        # session id -> {first-message hash -> conversation id}
        self._threads: dict[str, dict[str, str]] = {}
        self.transcripts = transcripts or TranscriptStore()

    @staticmethod
    def _first_seed(body: dict) -> str:
        """Text of the first user message — the thread's stable identity."""
        for message in body.get("messages") or []:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                seed = content
            elif isinstance(content, list):
                seed = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                continue
            if seed:
                return seed[:4096]
        return ""

    def _resolve_thread(self, request: ProxyRequest, body: dict) -> tuple[str, str, str]:
        """(conversation id, parent id, agent id) for this request's thread.

        Registers the thread on first sight. The first thread seen for a
        session claims the bare session id (the main conversation) unless
        its first message matches a sub-agent transcript; later threads
        become children (``<sid>#<hash>``) — except a /compact continuation,
        which is the main thread with a rewritten history and keeps its id.
        """
        sid = request.headers.get("X-Claude-Code-Session-Id", "")
        seed = self._first_seed(body)
        h = hashlib.sha256(seed.encode()).hexdigest()[:16] if seed else "empty"

        if sid:
            threads = self._threads.setdefault(sid, {})
            cid = threads.get(h)
            if cid is not None:
                conv = self.conversations.get(cid)
                return (cid, conv.parent_id if conv else "",
                        conv.agent_id if conv else "")

            agent_id = self.transcripts.find_agent_by_seed(sid, seed)
            if agent_id is None and threads and seed.lstrip().startswith(_COMPACT_PREFIX):
                # Main thread continuing after /compact under a new seed.
                threads[h] = sid
                return (sid, "", "")
            if agent_id is None and not threads:
                threads[h] = sid  # main conversation
                return (sid, "", "")
            cid = f"{sid}#{h[:8]}"
            threads[h] = cid
            return (cid, sid, agent_id or "")

        # No session header. Sub-agent calls may arrive header-less — try to
        # attribute them to a live session via the transcript on disk before
        # falling back to an anonymous bucket.
        if seed:
            for known_sid in list(self._threads.keys()):
                agent_id = self.transcripts.find_agent_by_seed(known_sid, seed)
                if agent_id:
                    threads = self._threads.setdefault(known_sid, {})
                    cid = threads.get(h)
                    if cid is None:
                        cid = f"{known_sid}#{h[:8]}"
                        threads[h] = cid
                    return (cid, known_sid, agent_id)
            digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
            return (f"anon-{digest}", "", "")
        return ("anon-unknown", "", "")

    def _session_id(self, request: ProxyRequest, body: dict) -> str:
        """Conversation id for this request (thread-aware). Kept as the
        single seam both request and response paths resolve through."""
        return self._resolve_thread(request, body)[0]

    def peek_thread_id(self, sid: str, body: dict) -> str:
        """Read-only thread lookup for other middlewares (cache simulator).

        Returns the registered conversation id for this request's thread,
        or the bare session id when the thread isn't registered (yet).
        """
        seed = self._first_seed(body)
        h = hashlib.sha256(seed.encode()).hexdigest()[:16] if seed else "empty"
        return self._threads.get(sid, {}).get(h, sid)

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        path = request.path.split("?")[0]
        if path != "/v1/messages":
            return request
        body = request.require_json()
        if not body:
            return request

        if body.get("max_tokens") == 1:
            return request

        sid, parent_id, agent_id = self._resolve_thread(request, body)
        now = time.time()

        if sid not in self.conversations:
            self.conversations[sid] = Conversation(
                id=sid,
                label=extract_label(body),
                fingerprint=sid,
                started_at=now,
                last_active=now,
                model=body.get("model", ""),
                parent_id=parent_id,
                agent_id=agent_id,
                first_seed=self._first_seed(body)[:1000],
            )

        conv = self.conversations[sid]
        conv.last_active = now
        conv.model = body.get("model", conv.model)
        if conv.parent_id and not conv.agent_id:
            # The sub-agent's transcript may not have existed when its first
            # request arrived — retry the attribution (cached, cheap).
            found = self.transcripts.find_agent_by_seed(conv.parent_id, conv.first_seed)
            if found:
                conv.agent_id = found

        # Re-run label extraction on every request: Claude Code's explicit
        # {"title": "..."} JSON arrives mid-conversation (not the first turn),
        # so we must keep looking until we find it. Once an explicit title
        # has been found (i.e. label no longer equals the first-turn heuristic
        # or "conversation"), don't downgrade it on later requests.
        new_label = extract_label(body)
        if new_label != "conversation":
            conv.label = new_label

        self._pending[id(request)] = {
            "chunks": [],
            "body": body,
            "request": request,
            "timestamp": now,
        }

        return request

    async def on_response_started(self, request: ProxyRequest, response: ProxyResponse) -> ProxyResponse:
        req_id = id(request)
        if req_id in self._pending:
            self._pending[req_id]["encoding"] = response.headers.get("Content-Encoding", "")
        return response

    async def on_response_chunk(self, request: ProxyRequest, chunk: bytes) -> bytes:
        req_id = id(request)
        if req_id in self._pending:
            self._pending[req_id]["chunks"].append(chunk)
        return chunk

    async def on_response_done(self, request: ProxyRequest, response: ProxyResponse):
        req_id = id(request)
        pending = self._pending.pop(req_id, None)
        if not pending:
            return

        body = pending["body"]
        orig_request = pending["request"]
        sid = self._session_id(orig_request, body)
        conv = self.conversations.get(sid)
        if not conv:
            return

        messages = body.get("messages", [])
        turns = parse_turns(messages)

        body_without_messages = {k: v for k, v in body.items() if k != "messages"}
        base_chars = len(json.dumps(body_without_messages))

        msg_index = 0
        for turn in turns:
            turn_end = msg_index
            saw_assistant = False
            while turn_end < len(messages):
                role = messages[turn_end].get("role", "")
                if role == "user" and saw_assistant:
                    break
                if role == "assistant":
                    saw_assistant = True
                turn_end += 1

            turn.chars_in = base_chars + len(json.dumps(messages[:turn_end]))
            turn.chars_out = sum(
                len(json.dumps(messages[mi].get("content", "")))
                for mi in range(msg_index, turn_end)
                if messages[mi].get("role") == "assistant"
            )
            turn._msg_range = (msg_index, turn_end)
            for mi in range(msg_index, turn_end):
                m = messages[mi]
                if m.get("role") != "user":
                    continue
                content = m.get("content", [])
                if not isinstance(content, list):
                    continue
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tid = item.get("tool_use_id", "")
                        if tid:
                            turn.tool_use_ids.append(tid)
            msg_index = turn_end

        # Compute bash tool_result chars per turn
        tool_names: dict[str, str] = {}
        tool_result_msg_bash: dict[str, int] = {}
        for mi, m in enumerate(messages):
            content = m.get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    tool_names[item.get("id", "")] = item.get("name", "")
                if item.get("type") == "tool_result":
                    tid = item.get("tool_use_id", "")
                    if tool_names.get(tid) == "Bash":
                        rc = item.get("content", "")
                        if isinstance(rc, str) and rc:
                            tool_result_msg_bash[tid] = (mi, len(rc))

        for turn in turns:
            start, end = turn._msg_range
            for tid, (mi, chars) in tool_result_msg_bash.items():
                if start <= mi < end:
                    turn.bash_chars += chars

        # Extract file operations (Read/Write/Edit) per turn.
        # Attribute to the turn containing the tool_result (not the tool_use),
        # so file ops align with the main chart's green "tool result" bars.
        from .models import FileOp
        _FILE_TOOLS = {"Read", "Write", "Edit"}
        # Phase 1: scan all tool_use blocks to build id→FileOp map
        file_op_by_id: dict[str, FileOp] = {}
        for mi, m in enumerate(messages):
            content = m.get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name", "")
                if name not in _FILE_TOOLS:
                    continue
                inp = item.get("input", {})
                if not isinstance(inp, dict):
                    continue
                fp = inp.get("file_path", "")
                if not fp:
                    continue
                op = FileOp(tool_name=name, file_path=fp)
                if name == "Read":
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    if offset is not None or limit is not None:
                        op.start_line = parse_int_param(offset, 0)
                        op.end_line = op.start_line + parse_int_param(limit, 2000)
                elif name == "Edit":
                    op.old_str_len = len(inp.get("old_string", ""))
                    op.new_str_len = len(inp.get("new_string", ""))
                elif name == "Write":
                    op.content_len = len(inp.get("content", ""))
                tool_id = item.get("id", "")
                if tool_id:
                    file_op_by_id[tool_id] = op
        # Phase 2: for each turn, find tool_result blocks and attach the FileOp
        for turn in turns:
            start, end = turn._msg_range
            for mi in range(start, end):
                m = messages[mi]
                content = m.get("content", [])
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_result":
                        continue
                    tid = item.get("tool_use_id", "")
                    op = file_op_by_id.get(tid)
                    if op:
                        turn.file_ops.append(op)

        # Extract cache_control directives per turn
        # Check system, tools, and messages for cache_control blocks
        system_cache_types = set()
        system = body.get("system", [])
        if isinstance(system, list):
            for item in system:
                if isinstance(item, dict) and item.get("cache_control"):
                    cc = item["cache_control"]
                    if isinstance(cc, dict) and "type" in cc:
                        system_cache_types.add(cc["type"])

        tools_cache_types = set()
        tools = body.get("tools", [])
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and tool.get("cache_control"):
                    cc = tool["cache_control"]
                    if isinstance(cc, dict) and "type" in cc:
                        tools_cache_types.add(cc["type"])

        for turn in turns:
            start, end = turn._msg_range
            cache_types = system_cache_types | tools_cache_types
            for mi in range(start, end):
                m = messages[mi]
                content = m.get("content", [])
                if not isinstance(content, list):
                    continue
                for item in content:
                    if isinstance(item, dict) and item.get("cache_control"):
                        cc = item["cache_control"]
                        if isinstance(cc, dict) and "type" in cc:
                            cache_types.add(cc["type"])
            turn.cache_control_types = sorted(cache_types)

        raw = b"".join(pending["chunks"])
        encoding = pending.get("encoding", "")
        text = decompress(raw, encoding)
        response_blocks, usage, response_chars = parse_sse_blocks(text)

        if response_blocks:
            if turns:
                turns[-1].blocks.extend(response_blocks)
                turns[-1].chars_out = len(text)
                turns[-1].assistant_text_chars += response_chars.get("assistant_text", 0)
                turns[-1].tool_call_chars += response_chars.get("tool_call", 0)
                turns[-1].thinking_chars += response_chars.get("thinking", 0)
            else:
                turns.append(Turn(index=0, label="(response)", blocks=response_blocks,
                                  chars_in=len(pending["request"].body), chars_out=len(text),
                                  assistant_text_chars=response_chars.get("assistant_text", 0),
                                  tool_call_chars=response_chars.get("tool_call", 0),
                                  thinking_chars=response_chars.get("thinking", 0)))

        # Restore previously accumulated token counts FIRST, then add current call on top
        old_token_data = {
            t.index: (t.input_tokens, t.output_tokens,
                      t.cache_read_input_tokens, t.cache_creation_input_tokens)
            for t in conv.turns
            if t.input_tokens or t.cache_read_input_tokens or t.cache_creation_input_tokens
        }
        for turn in turns:
            if turn.index in old_token_data:
                (turn.input_tokens, turn.output_tokens,
                 turn.cache_read_input_tokens, turn.cache_creation_input_tokens) = old_token_data[turn.index]

        if usage and turns:
            inp  = usage.get("input_tokens", 0)
            out  = usage.get("output_tokens", 0)
            cr   = usage.get("cache_read_input_tokens", 0)
            cc   = usage.get("cache_creation_input_tokens", 0)
            turns[-1].input_tokens             += inp
            turns[-1].output_tokens            += out
            turns[-1].cache_read_input_tokens  += cr
            turns[-1].cache_creation_input_tokens += cc

            # --- debug logging ---
            orig_body   = pending["body"]
            sent_body   = pending["request"].require_json()
            orig_msgs   = orig_body.get("messages", [])
            sent_msgs   = sent_body.get("messages", [])

            def _count_images(msgs):
                n = 0
                for m in msgs:
                    for item in (m.get("content") or []):
                        if not isinstance(item, dict): continue
                        if item.get("type") == "image": n += 1
                        if item.get("type") == "tool_result":
                            for b in (item.get("content") or []):
                                if isinstance(b, dict) and b.get("type") == "image": n += 1
                return n

            orig_imgs  = _count_images(orig_msgs)
            sent_imgs  = _count_images(sent_msgs)
            orig_kb    = len(json.dumps(orig_msgs)) // 1024
            sent_kb    = len(json.dumps(sent_msgs)) // 1024
            total_tok  = inp + cr + cc
            cache_pct  = int(cr * 100 / total_tok) if total_tok else 0
            logger.info(
                "tokens | turn=%d  sent=%dk(imgs=%d) orig=%dk(imgs=%d) | "
                "input=%d cache_read=%d cache_create=%d output=%d total=%d cache_pct=%d%%",
                turns[-1].index, sent_kb, sent_imgs, orig_kb, orig_imgs,
                inp, cr, cc, out, total_tok, cache_pct,
            )

        conv.turns = turns
        conv.breakdown = compute_breakdown(body)
        # Keep the raw system/tools of the latest request (pre-optimizer —
        # `body` was captured before the pipeline ran). The Explorer renders
        # these in full; transcripts never contain them.
        conv.system_raw = body.get("system")
        conv.tools_raw = body.get("tools")
        if conv.label == "conversation" and turns:
            from .parsing import _turn_label
            label = _turn_label(turns[0].blocks)
            if label != "(empty turn)":
                conv.label = label

        self._dump_conv_debug(conv, response_chars)

        # Notify UI driver (Mac menu or Textual TUI) of updated state.
        app_ref = getattr(self, "app_ref", None)
        if app_ref is not None:
            try:
                app_ref.notify_update()
            except Exception:
                pass

    def _dump_conv_debug(self, conv, response_chars: dict):
        # Always write to the user-writable log dir. The previous form,
        # Path(__file__).parent.parent / "logs", resolved to the dev tree
        # when running from source AND to inside the signed .app bundle when
        # running from the installed copy — the latter is read-only and fails
        # silently. Both paths now converge on ~/.voitta-desktop/logs/.
        logs_dir = Path.home() / ".voitta-desktop" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', conv.id)[:60]
        path = logs_dir / f"conv_{safe_id}.json"

        bd = conv.breakdown
        data = {
            "id": conv.id,
            "label": conv.label,
            "parent_id": conv.parent_id,
            "agent_id": conv.agent_id,
            "model": conv.model,
            "last_active": conv.last_active,
            "request_count": conv.request_count,
            "breakdown": {
                "system_prompt_chars": bd.system_prompt_chars if bd else 0,
                "tools_chars": bd.tools_chars if bd else 0,
                "tools_count": bd.tools_count if bd else 0,
                "messages_chars": bd.messages_chars if bd else 0,
                "other_chars": bd.other_chars if bd else 0,
            } if bd else None,
            "response_chars": response_chars,
            "turns": [],
        }
        for t in conv.turns:
            turn_data = {
                "index": t.index,
                "label": t.label,
                "chars_in": t.chars_in,
                "chars_out": t.chars_out,
                "user_text_chars": t.user_text_chars,
                "tool_result_chars": t.tool_result_chars,
                "assistant_text_chars": t.assistant_text_chars,
                "tool_call_chars": t.tool_call_chars,
                "image_chars": t.image_chars,
                "bash_chars": t.bash_chars,
                "thinking_chars": t.thinking_chars,
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "cache_read_input_tokens": t.cache_read_input_tokens,
                "cache_creation_input_tokens": t.cache_creation_input_tokens,
                "blocks": [
                    {"type": b.block_type.value, "summary": b.summary[:80]}
                    for b in t.blocks
                ],
                "file_ops": [
                    {"tool": op.tool_name, "file": op.file_path,
                     "start": op.start_line, "end": op.end_line}
                    for op in t.file_ops
                ],
            }
            data["turns"].append(turn_data)

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.debug("Dumped conv debug to %s", path)

    def get_conversation(self, conv_id: str):
        return self.conversations.get(conv_id)

    def get_conversations_sorted(self) -> list[Conversation]:
        return sorted(self.conversations.values(), key=lambda c: c.last_active, reverse=True)
