"""Build the chart-data payload for a conversation.

Extracted from the old per-conversation menu popup (ui/conv_menu.py) so the
Session Explorer's Stats pane renders the exact same chart. Two sources:

  • a live ``Conversation`` from the tracker (full fidelity), or
  • a ``conv_*.json`` debug dump from a previous run (degraded: no images,
    no cache simulation, summaries only) — used to seed the sidebar after
    a restart.

Both produce the ``(breakdown, turns)`` pair ``generate_chart_html`` expects.
"""

from __future__ import annotations


def build_chart_args(conv, optimizer_pipeline, cache_sim) -> tuple[dict, list[dict]]:
    """(breakdown, turns) chart payload for a live tracked conversation."""
    breakdown_data = {"system": 0, "tools": 0, "other": 0}
    turns_data: list[dict] = []

    bd = conv.breakdown
    if bd:
        breakdown_data = {
            "system": bd.system_prompt_chars,
            "tools": bd.tools_chars,
            "other": bd.other_chars,
            "tools_count": bd.tools_count,
            "tool_groups": [
                {"prefix": g.prefix, "count": g.count, "chars": g.total_chars}
                for g in bd.tool_groups[:8]
            ],
            "system_blocks": [
                {"preview": p[:60], "chars": c}
                for p, c in bd.system_blocks[:5]
            ],
        }

    stripped_ids = optimizer_pipeline.stripped_tool_ids
    stripped_msgs = optimizer_pipeline.stripped_msg_indices
    for t in conv.turns:
        images_data = []
        for img in t.images:
            token_chars = int(img.width * img.height / 750 * 3.5)
            images_data.append({
                "media_type": img.media_type,
                "base64_chars": img.base64_chars,
                "raw_bytes": img.raw_bytes,
                "width": img.width, "height": img.height,
                "source_type": img.source_type,
                "thumbnail": img.thumbnail_b64 if img.thumbnail_b64 else "",
                "token_chars": token_chars,
            })
        blocks_data = [
            {"type": b.block_type.value, "summary": b.summary[:100]}
            for b in t.blocks
        ]
        # Per-turn stripped chars from optimizer data
        stripped_tool = sum(
            stripped_ids.get(tid, 0) for tid in t.tool_use_ids
        )
        stripped_think = sum(
            stripped_msgs.get(mi, 0)
            for mi in range(t._msg_range[0], t._msg_range[1])
        )
        turns_data.append({
            "index": t.index, "label": t.label[:30],
            "user_text": t.user_text_chars,
            "tool_result": t.tool_result_chars,
            "assistant_text": t.assistant_text_chars,
            "tool_call": t.tool_call_chars,
            "image": sum(int(img.width * img.height / 750 * 3.5) for img in t.images),
            "bash": t.bash_chars,
            "thinking": t.thinking_chars,
            "stripped_tool": stripped_tool,
            "stripped_thinking": stripped_think,
            "images": images_data,
            "blocks": blocks_data,
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "cache_read_input_tokens": t.cache_read_input_tokens,
            "cache_creation_input_tokens": t.cache_creation_input_tokens,
            "cache_control_types": t.cache_control_types,
            "msg_count": t._msg_range[1] - t._msg_range[0],
            "file_ops": [
                {
                    "tool": op.tool_name,
                    "file": op.file_path,
                    "start": op.start_line,
                    "end": op.end_line,
                    "old_len": op.old_str_len,
                    "new_len": op.new_str_len,
                    "content_len": op.content_len,
                }
                for op in t.file_ops
            ],
        })

    # Align simulated-cache data from the end (cache resets on restart,
    # turns don't).
    cache_history = cache_sim.get_history(conv.id) if cache_sim else []
    ch_offset = len(turns_data) - len(cache_history)
    for i, td in enumerate(turns_data):
        ci = i - ch_offset
        td["cache_sim"] = cache_history[ci] if 0 <= ci < len(cache_history) else None

    return breakdown_data, turns_data


def build_chart_args_from_dump(dump: dict) -> tuple[dict, list[dict]]:
    """(breakdown, turns) from a conv_*.json debug dump — a previous run's
    last-known state. Missing detail (images, cache sim, stripped chars)
    defaults to empty so the chart template renders without live data."""
    bd = dump.get("breakdown") or {}
    breakdown_data = {
        "system": bd.get("system_prompt_chars", 0),
        "tools": bd.get("tools_chars", 0),
        "other": bd.get("other_chars", 0),
        "tools_count": bd.get("tools_count", 0),
        "tool_groups": [],
        "system_blocks": [],
    }
    turns_data = []
    for t in dump.get("turns") or []:
        turns_data.append({
            "index": t.get("index", 0),
            "label": str(t.get("label", ""))[:30],
            "user_text": t.get("user_text_chars", 0),
            "tool_result": t.get("tool_result_chars", 0),
            "assistant_text": t.get("assistant_text_chars", 0),
            "tool_call": t.get("tool_call_chars", 0),
            "image": t.get("image_chars", 0),
            "bash": t.get("bash_chars", 0),
            "thinking": t.get("thinking_chars", 0),
            "stripped_tool": 0,
            "stripped_thinking": 0,
            "images": [],
            "blocks": t.get("blocks") or [],
            "input_tokens": t.get("input_tokens", 0),
            "output_tokens": t.get("output_tokens", 0),
            "cache_read_input_tokens": t.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": t.get("cache_creation_input_tokens", 0),
            "cache_control_types": [],
            "msg_count": 0,
            "file_ops": [
                {"tool": op.get("tool", ""), "file": op.get("file", ""),
                 "start": op.get("start"), "end": op.get("end"),
                 "old_len": op.get("old_len", 0), "new_len": op.get("new_len", 0),
                 "content_len": op.get("content_len", 0)}
                for op in (t.get("file_ops") or [])
            ],
            "cache_sim": None,
        })
    return breakdown_data, turns_data
