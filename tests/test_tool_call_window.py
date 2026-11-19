"""The tool-call window: a long agent run with one human message gets trimmed too.

On 2026-11-18 a 100-email voitta-kvm run ("go on") had one human message and
~500 tool round-trips; counted in human turns nothing was ever old, so every
screenshot and screen listing went upstream whole until the context filled.
These tests pin the window (optimizers.TOOL_CALL_KEEP / TOOL_CALL_BLOCK): old
results go, the trimmed prefix stays byte-identical between block steps (the
prompt cache), pairing stays valid, and assistant-side content is untouched.
"""

from __future__ import annotations

import asyncio
import base64
import json

from middleware.base import ProxyRequest
from optimizers import (TOOL_CALL_BLOCK, TOOL_CALL_KEEP, OptimizerPipeline, configure_tool_window,
                        tool_call_cut, validate_tool_pairing)
from optimizers.bash_compress import BashCompressor
from optimizers.image import ImageOptimizer, vt_object_store
from optimizers.thinking import ThinkingOptimizer
from optimizers.tool_result import ToolResultOptimizer
from optimizers.tool_use import ToolUseOptimizer

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(20000)).decode()

def _pipeline(**window) -> OptimizerPipeline:
    opts = [BashCompressor(), ToolUseOptimizer(keep_turns=5, min_chars=500), ToolResultOptimizer(keep_turns=5),
            ImageOptimizer(keep_turns=5), ThinkingOptimizer(keep_turns=5)]
    if window:
        configure_tool_window(opts, window)
    return OptimizerPipeline(opts)

def _run(n_calls: int, human_image: bool = False) -> list:
    """One human message, then n_calls KVM-like round-trips (thinking + tool_use, result with text + image)."""
    first = [{"type": "text", "text": "go on! send the entire 100"}]
    if human_image:
        first.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}})
    msgs = [{"role": "user", "content": first}]
    for i in range(n_calls):
        tid = f"toolu_{i:04d}"
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"step {i}", "signature": f"sig{i}"},
            {"type": "tool_use", "id": tid, "name": "mcp__voitta__kvm_key_sequence",
             "input": {"steps": [{"text": f"letter {i} " + "x" * 600}]}},
        ]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": [
            {"type": "text", "text": f"[Gaba Mac] verdict: ok. call {i}\n" + "Refs line\n" * 700},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": PNG}},
        ]}]})
    return msgs

def _optimize(msgs: list, pipe: OptimizerPipeline | None = None) -> list:
    req = ProxyRequest(method="POST", path="/v1/messages", headers={}, body=json.dumps(
        {"model": "claude-opus-5-5", "messages": msgs}).encode())
    req = asyncio.run((pipe or _pipeline()).on_request(req))
    return req.json["messages"]

def _whole(msgs: list) -> list[int]:
    """Indices (round-trip numbers) of tool results still whole (text over 1000 chars and the image)."""
    out = []
    n = 0
    for m in msgs:
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_result":
                c = b["content"]
                if isinstance(c, list) and any(x.get("type") == "image" for x in c):
                    out.append(n)
                n += 1
    return out

def test_cut_moves_only_in_whole_blocks():
    cuts = {}
    for n in range(0, 200):
        cuts[n] = tool_call_cut(_run(n)[:1 + 2 * n] if n else [{"role": "user", "content": "hi"}])
    assert all(cuts[n] is None for n in range(TOOL_CALL_KEEP + TOOL_CALL_BLOCK))
    changes = [n for n in range(1, 200) if cuts[n] != cuts[n - 1]]
    assert changes == list(range(TOOL_CALL_KEEP + TOOL_CALL_BLOCK, 200, TOOL_CALL_BLOCK))

def test_long_run_with_one_human_message_is_trimmed():
    vt_object_store.clear()
    msgs = _run(300)
    before = len(json.dumps(msgs))
    out = _optimize(msgs)
    whole = _whole(out)
    assert TOOL_CALL_KEEP <= len(whole) < TOOL_CALL_KEEP + TOOL_CALL_BLOCK, len(whole)
    assert whole == list(range(300 - len(whole), 300)), "only the most recent round-trips stay whole"
    after = len(json.dumps(out))
    assert after < before / 5, (before, after)
    assert validate_tool_pairing(out) == []
    # a removed result's placeholder keeps its verdict line and a retrievable hash
    first = out[2]["content"][0]["content"]
    assert isinstance(first, str) and "verdict: ok. call 0" in first and "get_vt_object" in first

def test_assistant_side_of_the_run_is_untouched():
    """Thinking and tool_use arguments belong to the run in progress: the window leaves them."""
    msgs = _run(120)
    out = _optimize(msgs)
    for a, b in zip(msgs, out):
        if a["role"] == "assistant":
            assert a == b

def test_human_image_in_the_run_in_progress_is_kept():
    msgs = _run(120, human_image=True)
    out = _optimize(msgs)
    assert any(b.get("type") == "image" for b in out[0]["content"])

def test_consecutive_requests_share_the_trimmed_prefix_between_block_steps():
    """The cache: request n+1's trimmed view starts with request n's, byte for byte, unless the cut moved."""
    full = _run(120)
    views = {n: _optimize(full[:1 + 2 * n]) for n in range(60, 100)}
    moved = []
    for n in range(60, 99):
        a, b = views[n], views[n + 1]
        same = json.dumps(a) == json.dumps(b[:len(a)])
        if not same:
            moved.append(n + 1)
    assert moved == [n for n in range(61, 100) if (n - TOOL_CALL_KEEP) % TOOL_CALL_BLOCK == 0], moved

def test_window_can_be_turned_off():
    msgs = _run(120)
    out = _optimize(msgs, _pipeline(tool_call_keep=0, tool_call_block=20))
    assert len(_whole(out)) == 120

def test_short_runs_are_left_alone():
    msgs = _run(TOOL_CALL_KEEP + TOOL_CALL_BLOCK - 1)
    out = _optimize(msgs)
    assert len(_whole(out)) == TOOL_CALL_KEEP + TOOL_CALL_BLOCK - 1
