"""Helpers for parsing request bodies and SSE response streams into turns and blocks."""

import json
import re
import time

from .models import (
    BlockType, ContentBlock, Turn, ToolGroup, BodyBreakdown, ImageInfo,
    classify_tool, extract_image_info,
)


# ── Text helpers ─────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Strip system-reminder blocks, XML-like tags, and collapse whitespace."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_SYSTEM_JUNK_PATTERNS = [
    re.compile(r"^#\s*(MCP|System|Tool|Environment)", re.IGNORECASE),
    re.compile(r"^(The following|You are connected|Available skills)", re.IGNORECASE),
    re.compile(r"^(Today's date|currentDate|IMPORTANT:)", re.IGNORECASE),
    re.compile(r"^The user (opened|selected|is viewing)", re.IGNORECASE),
    re.compile(r"^Note:\s*/\S+\s+was modified", re.IGNORECASE),
]


def is_system_junk(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return True
    for pat in _SYSTEM_JUNK_PATTERNS:
        if pat.search(cleaned):
            return True
    return False


def truncate(text: str, maxlen: int = 80) -> str:
    if len(text) > maxlen:
        return text[:maxlen] + "..."
    return text


# ── Message parsing ──────────────────────────────────────────────────────────

def find_tool_name(messages: list, tool_use_id: str) -> str:
    """Find the tool name for a tool_use_id by scanning assistant messages."""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("id") == tool_use_id):
                return block.get("name", "unknown_tool")
    return "tool"


def parse_message_blocks(msg: dict, messages: list) -> list[ContentBlock]:
    """Parse a single message into content blocks."""
    blocks = []
    now = time.time()
    role = msg.get("role", "")
    content = msg.get("content", "")

    if isinstance(content, str):
        text = clean_text(content)
        if role == "user" and is_system_junk(text):
            return blocks
        if text:
            bt = BlockType.USER_TEXT if role == "user" else BlockType.ASSISTANT_TEXT
            blocks.append(ContentBlock(block_type=bt, summary=truncate(text), timestamp=now))
        return blocks

    if not isinstance(content, list):
        return blocks

    for item in content:
        if not isinstance(item, dict):
            continue
        btype = item.get("type", "")

        if btype == "text":
            text = clean_text(item.get("text", ""))
            if not text or (role == "user" and is_system_junk(text)):
                continue
            bt = BlockType.USER_TEXT if role == "user" else BlockType.ASSISTANT_TEXT
            blocks.append(ContentBlock(block_type=bt, summary=truncate(text), timestamp=now))

        elif btype == "thinking":
            thinking = item.get("thinking", "")
            if thinking:
                blocks.append(ContentBlock(
                    block_type=BlockType.THINKING,
                    summary=truncate(clean_text(thinking)), timestamp=now))

        elif btype == "tool_use":
            name = item.get("name", "tool")
            tool_type = classify_tool(name)
            inp = item.get("input", {})
            input_summary = ""
            if isinstance(inp, dict):
                for key in ("command", "query", "file_path", "pattern"):
                    if key in inp:
                        input_summary = truncate(str(inp[key]), 50)
                        break
            summary = name + (f"({input_summary})" if input_summary else "")
            blocks.append(ContentBlock(
                block_type=tool_type, summary=truncate(summary), timestamp=now))

        elif btype == "tool_result":
            tool_use_id = item.get("tool_use_id", "")
            tool_name = find_tool_name(messages, tool_use_id)
            result_content = item.get("content", "")
            if isinstance(result_content, list):
                texts = [b.get("text", "") for b in result_content
                         if isinstance(b, dict) and b.get("type") == "text"]
                result_content = " ".join(texts)
            if isinstance(result_content, str):
                result_content = clean_text(result_content)
            is_error = item.get("is_error", False)
            status = "error" if is_error else "ok"
            summary = f"{tool_name} → {status}"
            if result_content:
                summary += f": {truncate(result_content, 60)}"
            blocks.append(ContentBlock(
                block_type=BlockType.TOOL_RESULT, summary=truncate(summary), timestamp=now))

        elif btype == "server_tool_use":
            name = item.get("name", "server_tool")
            blocks.append(ContentBlock(
                block_type=BlockType.SERVER_TOOL_CALL, summary=name, timestamp=now))

    return blocks


def count_message_chars(msg: dict) -> int:
    """Count total characters in a message's content."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            btype = item.get("type", "")
            if btype == "text":
                total += len(item.get("text", ""))
            elif btype == "thinking":
                total += len(item.get("thinking", ""))
            elif btype == "tool_use":
                total += len(json.dumps(item.get("input", {})))
            elif btype == "tool_result":
                rc = item.get("content", "")
                if isinstance(rc, str):
                    total += len(rc)
                elif isinstance(rc, list):
                    for b in rc:
                        if isinstance(b, dict) and b.get("type") == "text":
                            total += len(b.get("text", ""))
        return total
    return 0


def count_message_chars_by_type(msg: dict) -> tuple[dict, list[ImageInfo]]:
    """Count characters by content type for a single message.

    Returns (counts_dict, images_list) where counts has keys:
    user_text, tool_result, assistant_text, tool_call, image
    """
    counts = {"user_text": 0, "tool_result": 0, "assistant_text": 0, "tool_call": 0, "image": 0, "thinking": 0}
    images: list[ImageInfo] = []
    role = msg.get("role", "")
    content = msg.get("content", "")

    if isinstance(content, str):
        if role == "user":
            counts["user_text"] = len(content)
        else:
            counts["assistant_text"] = len(content)
        return counts, images

    if not isinstance(content, list):
        return counts, images

    for item in content:
        if not isinstance(item, dict):
            continue
        btype = item.get("type", "")

        if btype == "text":
            chars = len(item.get("text", ""))
            if role == "user":
                counts["user_text"] += chars
            else:
                counts["assistant_text"] += chars

        elif btype == "thinking":
            counts["thinking"] += len(item.get("thinking", ""))

        elif btype == "tool_use":
            counts["tool_call"] += len(json.dumps(item.get("input", {})))

        elif btype == "tool_result":
            rc = item.get("content", "")
            if isinstance(rc, str):
                counts["tool_result"] += len(rc)
            elif isinstance(rc, list):
                for b in rc:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        counts["tool_result"] += len(b.get("text", ""))
                    elif b.get("type") == "image":
                        src = b.get("source", {})
                        counts["image"] += len(src.get("data", ""))
                        info = extract_image_info(b)
                        if info:
                            images.append(info)

        elif btype == "image":
            src = item.get("source", {})
            counts["image"] += len(src.get("data", ""))
            info = extract_image_info(item)
            if info:
                images.append(info)

    return counts, images


def parse_turns(messages: list) -> list[Turn]:
    """Parse the full messages array into turns.

    A turn is: one or more user messages followed by one or more assistant messages.
    """
    turns = []
    current_blocks = []
    current_role = None
    current_chars_in = 0
    current_chars_out = 0
    current_breakdown = {"user_text": 0, "tool_result": 0, "assistant_text": 0, "tool_call": 0, "image": 0, "thinking": 0}
    current_images: list[ImageInfo] = []
    turn_index = 0

    for msg in messages:
        role = msg.get("role", "")
        blocks = parse_message_blocks(msg, messages)
        chars = count_message_chars(msg)
        by_type, msg_images = count_message_chars_by_type(msg)

        if role == "user" and current_role == "assistant" and current_blocks:
            label = _turn_label(current_blocks)
            turns.append(Turn(
                index=turn_index, label=label, blocks=current_blocks,
                chars_in=current_chars_in, chars_out=current_chars_out,
                user_text_chars=current_breakdown["user_text"],
                tool_result_chars=current_breakdown["tool_result"],
                assistant_text_chars=current_breakdown["assistant_text"],
                tool_call_chars=current_breakdown["tool_call"],
                image_chars=current_breakdown["image"],
                thinking_chars=current_breakdown["thinking"],
                images=current_images,
            ))
            turn_index += 1
            current_blocks = []
            current_chars_in = 0
            current_chars_out = 0
            current_breakdown = {"user_text": 0, "tool_result": 0, "assistant_text": 0, "tool_call": 0, "image": 0, "thinking": 0}
            current_images = []

        current_blocks.extend(blocks)
        current_images.extend(msg_images)
        for k in current_breakdown:
            current_breakdown[k] += by_type.get(k, 0)
        if role == "user":
            current_chars_in += chars
        elif role == "assistant":
            current_chars_out += chars
        current_role = role

    if current_blocks:
        label = _turn_label(current_blocks)
        turns.append(Turn(
            index=turn_index, label=label, blocks=current_blocks,
            chars_in=current_chars_in, chars_out=current_chars_out,
            user_text_chars=current_breakdown["user_text"],
            tool_result_chars=current_breakdown["tool_result"],
            assistant_text_chars=current_breakdown["assistant_text"],
            tool_call_chars=current_breakdown["tool_call"],
            image_chars=current_breakdown["image"],
            images=current_images,
        ))

    return turns


def _turn_label(blocks: list[ContentBlock]) -> str:
    for b in blocks:
        if b.block_type == BlockType.USER_TEXT and b.summary != "─── current turn ───":
            return b.summary
    if blocks:
        return blocks[0].summary
    return "(empty turn)"


def compute_breakdown(body: dict) -> BodyBreakdown:
    """Compute a breakdown of the request body sizes."""
    bd = BodyBreakdown()

    system = body.get("system", "")
    system_json = json.dumps(system)
    bd.system_prompt_chars = len(system_json)
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                text = block.get("text", "")
                preview = re.sub(r"\s+", " ", text[:80]).strip()
                if len(text) > 80:
                    preview += "..."
                bd.system_blocks.append((preview, len(text)))
    elif isinstance(system, str):
        preview = re.sub(r"\s+", " ", system[:80]).strip()
        bd.system_blocks.append((preview, len(system)))

    tools = body.get("tools", [])
    tools_json = json.dumps(tools)
    bd.tools_chars = len(tools_json)
    bd.tools_count = len(tools)

    groups: dict[str, list[tuple[str, int]]] = {}
    for tool in tools:
        name = tool.get("name", "?")
        chars = len(json.dumps(tool))
        if name.startswith("mcp__"):
            parts = name.split("__")
            prefix = "__".join(parts[:2])
        else:
            prefix = "Built-in"
        groups.setdefault(prefix, []).append((name, chars))

    for prefix, tool_list in groups.items():
        tool_list.sort(key=lambda x: x[1], reverse=True)
        total = sum(c for _, c in tool_list)
        bd.tool_groups.append(ToolGroup(
            prefix=prefix, count=len(tool_list),
            total_chars=total, tools=tool_list,
        ))
    bd.tool_groups.sort(key=lambda g: g.total_chars, reverse=True)

    messages = body.get("messages", [])
    bd.messages_chars = len(json.dumps(messages))

    full_body_chars = len(json.dumps(body))
    bd.other_chars = full_body_chars - bd.system_prompt_chars - bd.tools_chars - bd.messages_chars
    bd.total_chars = bd.system_prompt_chars + bd.tools_chars + bd.messages_chars + bd.other_chars

    return bd


def extract_label(body: dict) -> str:
    """Extract conversation label from the first real user message."""
    messages = body.get("messages", [])
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = clean_text(content)
            if text and not is_system_junk(text):
                return truncate(text, 60)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = clean_text(item.get("text", ""))
                    if text and not is_system_junk(text):
                        return truncate(text, 60)
    return "conversation"


# ── SSE response parsing ────────────────────────────────────────────────────

def parse_sse_blocks(text: str) -> tuple[list[ContentBlock], dict, dict]:
    """Parse SSE stream into content blocks, usage dict, and char counts by type."""
    blocks = []
    usage = {}
    response_chars = {"assistant_text": 0, "tool_call": 0, "thinking": 0}
    now = time.time()

    block_types = {}
    block_parts = {}
    block_names = {}
    current_index = None

    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except (json.JSONDecodeError, KeyError):
            continue

        evt_type = data.get("type", "")

        if evt_type == "message_start" and "message" in data:
            msg_usage = data["message"].get("usage")
            if msg_usage:
                usage.update(msg_usage)

        elif evt_type == "message_delta" and "usage" in data:
            usage.update(data["usage"])

        elif evt_type == "content_block_start":
            idx = data.get("index", 0)
            cb = data.get("content_block", {})
            cb_type = cb.get("type", "")
            block_types[idx] = cb_type
            block_parts[idx] = []
            current_index = idx
            if cb_type == "tool_use":
                block_names[idx] = cb.get("name", "tool")
            elif cb_type == "server_tool_use":
                block_names[idx] = cb.get("name", "server_tool")

        elif evt_type == "content_block_delta":
            idx = data.get("index", current_index or 0)
            delta = data.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                block_parts.setdefault(idx, []).append(delta.get("text", ""))
            elif delta_type == "thinking_delta":
                block_parts.setdefault(idx, []).append(delta.get("thinking", ""))
            elif delta_type == "input_json_delta":
                block_parts.setdefault(idx, []).append(delta.get("partial_json", ""))

    for idx in sorted(block_types.keys()):
        cb_type = block_types[idx]
        text_content = clean_text("".join(block_parts.get(idx, [])))

        if cb_type == "thinking":
            response_chars["thinking"] += len(text_content)
            blocks.append(ContentBlock(
                block_type=BlockType.THINKING,
                summary=truncate(text_content) if text_content else "(thinking)",
                timestamp=now,
            ))

        elif cb_type == "text":
            response_chars["assistant_text"] += len(text_content)
            if text_content:
                blocks.append(ContentBlock(
                    block_type=BlockType.ASSISTANT_TEXT,
                    summary=truncate(text_content),
                    timestamp=now,
                ))

        elif cb_type in ("tool_use", "server_tool_use"):
            name = block_names.get(idx, "tool")
            tool_type = classify_tool(name) if cb_type == "tool_use" else BlockType.SERVER_TOOL_CALL
            input_text = "".join(block_parts.get(idx, []))
            response_chars["tool_call"] += len(name) + len(input_text)
            input_summary = ""
            try:
                input_json = json.loads(input_text)
                if "command" in input_json:
                    input_summary = truncate(str(input_json["command"]), 50)
                elif "query" in input_json:
                    input_summary = truncate(str(input_json["query"]), 50)
                elif "file_path" in input_json:
                    input_summary = input_json["file_path"]
                elif "pattern" in input_json:
                    input_summary = truncate(str(input_json["pattern"]), 50)
            except (json.JSONDecodeError, KeyError):
                pass

            summary = name
            if input_summary:
                summary += f"({input_summary})"
            blocks.append(ContentBlock(
                block_type=tool_type,
                summary=truncate(summary),
                timestamp=now,
            ))

    # Fallback: non-streaming JSON response
    if not blocks and not block_types:
        try:
            resp_json = json.loads(text)
            if "usage" in resp_json:
                usage.update(resp_json["usage"])
            for item in resp_json.get("content", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    t = clean_text(item.get("text", ""))
                    response_chars["assistant_text"] += len(t)
                    if t:
                        blocks.append(ContentBlock(
                            block_type=BlockType.ASSISTANT_TEXT,
                            summary=truncate(t), timestamp=now))
                elif item.get("type") == "tool_use":
                    name = item.get("name", "tool")
                    input_str = json.dumps(item.get("input", {}))
                    response_chars["tool_call"] += len(name) + len(input_str)
                    blocks.append(ContentBlock(
                        block_type=classify_tool(name),
                        summary=truncate(name), timestamp=now))
        except (json.JSONDecodeError, KeyError):
            pass

    return blocks, usage, response_chars
