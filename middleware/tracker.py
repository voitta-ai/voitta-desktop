"""Conversation tracker middleware — maintains per-session turn history."""

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from .base import Middleware, ProxyRequest, ProxyResponse, decompress
from .models import Conversation, Turn
from .parsing import (
    parse_turns, compute_breakdown, extract_label, parse_sse_blocks,
)

logger = logging.getLogger("voitta-desktop.tracker")


class ConversationTracker(Middleware):
    """Tracks conversations with detailed content block history."""

    def __init__(self):
        self.conversations: dict[str, Conversation] = {}
        self._pending: dict[int, dict] = {}

    def _session_id(self, request: ProxyRequest, body: dict) -> str:
        session_id = request.headers.get("X-Claude-Code-Session-Id", "")
        if session_id:
            return session_id

        system = body.get("system", "")
        if isinstance(system, list):
            parts = []
            for block in system:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            system = "\n".join(parts)

        system = re.sub(r"<system-reminder>.*?</system-reminder>", "", system, flags=re.DOTALL)
        system = re.sub(r"(?m)^.*(?:Today's date|currentDate|Current date).*$", "", system)
        system = re.sub(r"\s+", " ", system).strip()

        return hashlib.sha256(system.encode()).hexdigest()[:16]

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        body = request.json
        path = request.path.split("?")[0]
        if not body or path != "/v1/messages":
            return request

        if body.get("max_tokens") == 1:
            return request

        sid = self._session_id(request, body)
        now = time.time()

        if sid not in self.conversations:
            self.conversations[sid] = Conversation(
                id=sid,
                label=extract_label(body),
                fingerprint=sid,
                started_at=now,
                last_active=now,
                model=body.get("model", ""),
            )

        conv = self.conversations[sid]
        conv.last_active = now
        conv.model = body.get("model", conv.model)

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
            msg_index = turn_end

        # Compute stale read chars per turn
        try:
            from optimizers.file_read import analyze_stale_reads
            stale = analyze_stale_reads(messages)
            if stale:
                tool_result_msg: dict[str, int] = {}
                for mi, m in enumerate(messages):
                    if m.get("role") != "user":
                        continue
                    content = m.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_result_msg[item.get("tool_use_id", "")] = mi

                for turn in turns:
                    start, end = turn._msg_range
                    for tid, chars in stale.items():
                        result_mi = tool_result_msg.get(tid)
                        if result_mi is not None and start <= result_mi < end:
                            turn.stale_read_chars += chars
        except Exception as e:
            logger.debug("stale read analysis failed: %s", e)

        # Compute bash tool_result chars per turn
        try:
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
        except Exception as e:
            logger.debug("bash chars analysis failed: %s", e)

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
            sent_body   = pending["request"].json or {}
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
        if conv.label == "conversation" and turns:
            from .parsing import _turn_label
            label = _turn_label(turns[0].blocks)
            if label != "(empty turn)":
                conv.label = label

        self._dump_conv_debug(conv, response_chars)

    def _dump_conv_debug(self, conv, response_chars: dict):
        try:
            logs_dir = Path(__file__).parent.parent / "logs"
            logs_dir.mkdir(exist_ok=True)
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', conv.id)[:60]
            path = logs_dir / f"conv_{safe_id}.json"

            bd = conv.breakdown
            data = {
                "id": conv.id,
                "label": conv.label,
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
                    "stale_read_chars": t.stale_read_chars,
                    "bash_chars": t.bash_chars,
                    "thinking_chars": t.thinking_chars,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "blocks": [
                        {"type": b.block_type.value, "summary": b.summary[:80]}
                        for b in t.blocks
                    ],
                }
                data["turns"].append(turn_data)

            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            logger.debug("Dumped conv debug to %s", path)
        except Exception as e:
            logger.warning("Failed to dump conv debug: %s", e)

    def get_conversation(self, conv_id: str):
        return self.conversations.get(conv_id)

    def get_conversations_sorted(self) -> list[Conversation]:
        return sorted(self.conversations.values(), key=lambda c: c.last_active, reverse=True)
