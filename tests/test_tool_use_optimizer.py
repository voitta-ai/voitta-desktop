"""Tests for the tool_use pair-collapser — focused on the two things that can
silently break it in production:

  1. CONTEXT CORRUPTION — collapsing a tool_use must never orphan its
     tool_result (or vice versa), never leave a fake-schema tool_use, never drop
     a message or lose human text, and must stay reconstructable via the stored
     reference.

  2. CACHE BEHAVIOR — Claude Code's context is append-only and the proxy
     re-derives the optimized view from scratch every turn. For the Anthropic
     prompt cache to hit, the optimized bytes of any already-settled prefix MUST
     be identical turn-over-turn. A single byte of drift in the stable zone
     re-writes the whole suffix at 1.25x every turn = catastrophic. These tests
     grow a conversation turn by turn and assert the settled prefix is frozen.

Run (no pytest needed):
    .venv/bin/python tests/test_tool_use_optimizer.py
"""

import asyncio
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from middleware.base import ProxyRequest
from optimizers import OptimizerPipeline
from optimizers.bash_compress import BashCompressor
from optimizers.image import ImageOptimizer, vt_object_store
from optimizers.thinking import ThinkingOptimizer
from optimizers.tool_result import ToolResultOptimizer
from optimizers.tool_use import ToolUseOptimizer

KEEP = 5
MIN_CHARS = 500
SID = "test-session"


# ───────────────────────── conversation simulator ─────────────────────────
class Conversation:
    """Builds an append-only Claude Code context, turn by turn.

    Mirrors how CC sends the full untrimmed history every request; the proxy
    re-optimizes from scratch each time. ``body()`` returns the current raw
    (unoptimized) request body.
    """

    def __init__(self):
        self.messages = []
        self._uid = 0

    def human(self, text):
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        return self

    def assistant_text(self, text):
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        return self

    def tool_call(self, name, command, result, parallel=None):
        """Assistant makes one (or several parallel) tool calls; user returns results."""
        calls = [(name, command, result)]
        if parallel:
            calls.extend(parallel)
        tu_blocks, tr_blocks = [], []
        for nm, cmd, res in calls:
            self._uid += 1
            tid = f"toolu_{self._uid:04d}"
            tu_blocks.append({"type": "tool_use", "id": tid, "name": nm,
                              "input": {"command": cmd} if nm == "Bash" else cmd})
            tr_blocks.append({"type": "tool_result", "tool_use_id": tid, "content": res})
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": "working"}] + tu_blocks})
        self.messages.append({"role": "user", "content": tr_blocks})
        return self

    def body(self):
        return {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 256,
            "system": [{"type": "text", "text": "You are a coding agent."}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            "messages": [json.loads(json.dumps(m)) for m in self.messages],  # deep copy
        }


def make_pipeline():
    return OptimizerPipeline(
        [BashCompressor(),
         ToolUseOptimizer(keep_turns=KEEP, min_chars=MIN_CHARS),
         ToolResultOptimizer(keep_turns=KEEP),
         ImageOptimizer(keep_turns=KEEP),
         ThinkingOptimizer(keep_turns=KEEP)],
        enabled=True,
    )


def optimize(body, pipeline=None):
    """Run a body through a fresh pipeline; return the optimized body dict."""
    pipeline = pipeline or make_pipeline()
    req = ProxyRequest(method="POST", path="/v1/messages",
                       headers={"X-Claude-Code-Session-Id": SID},
                       body=json.dumps(body).encode())
    out = asyncio.new_event_loop().run_until_complete(pipeline.on_request(req))
    return out.json


# ─────────────────────────────── helpers ──────────────────────────────────
def iter_blocks(body):
    for mi, m in enumerate(body["messages"]):
        c = m.get("content")
        if isinstance(c, list):
            for bi, b in enumerate(c):
                if isinstance(b, dict):
                    yield mi, bi, m.get("role"), b


def tool_use_ids(body):
    return {b["id"] for _, _, _, b in iter_blocks(body)
            if b.get("type") == "tool_use" and "id" in b}


def tool_result_ids(body):
    return {b["tool_use_id"] for _, _, _, b in iter_blocks(body)
            if b.get("type") == "tool_result" and "tool_use_id" in b}


def ser(messages):
    return json.dumps(messages, separators=(",", ":"), sort_keys=True)


def _strip_cc(obj):
    """Deep-remove cache_control directives.

    Anthropic keys its prompt cache on *content*, not on the cache_control
    breakpoint markers (the codebase's own CacheSimulator does the same strip,
    with the comment 'cache_control is a directive, not content'). For a faithful
    cache-stability comparison we must ignore where the moving breakpoint
    currently sits and compare only the content bytes.
    """
    if isinstance(obj, dict):
        return {k: _strip_cc(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_cc(x) for x in obj]
    return obj


def ser_content(msg):
    """Serialize a message's content for cache comparison, ignoring the
    (freely-moving) cache_control breakpoint marker."""
    return json.dumps(_strip_cc(msg), separators=(",", ":"), sort_keys=True)


# ──────────────────────────────── tests ───────────────────────────────────
def test_no_orphaned_tool_results():
    """Every surviving tool_result must still have its matching tool_use.
    Collapsing a pair must remove BOTH sides or NEITHER."""
    c = Conversation().human("start")
    for i in range(12):
        c.tool_call("Bash", "echo " + "A" * 900, "out " * 200)  # long call -> collapses
        c.human(f"next {i}")
    body = optimize(c.body())
    tu, tr = tool_use_ids(body), tool_result_ids(body)
    orphaned = tr - tu
    assert not orphaned, f"orphaned tool_results with no matching tool_use: {orphaned}"


def test_no_fake_tool_schema():
    """No surviving tool_use may carry a synthetic placeholder input — that's
    the fake-schema bug we explicitly avoid (model could learn to imitate it)."""
    c = Conversation().human("start")
    for i in range(10):
        c.tool_call("Bash", "echo " + "B" * 900, "result " * 100)
        c.human(f"q{i}")
    body = optimize(c.body())
    for _, _, _, b in iter_blocks(body):
        if b.get("type") == "tool_use":
            inp = b.get("input")
            assert not (isinstance(inp, dict) and ("_ref" in inp or "_note" in inp)), \
                f"fake-schema tool_use leaked into context: {inp}"


def test_no_cache_thrash_each_message_settles_once():
    """THE cache test (content-faithful). Grow the conversation turn by turn and
    track every message position's CONTENT bytes (cache_control stripped, since
    Anthropic keys the cache on content, not on the moving breakpoint marker).

    Invariant: each message changes content AT MOST ONCE over its whole life —
    the raw->collapsed transition as it ages past keep_turns — and then is frozen
    forever. More than one content change at a position = the prefix gets
    re-written repeatedly = cache thrash."""
    c = Conversation().human("start")
    distinct = {}     # position -> list of distinct consecutive content forms
    for t in range(25):
        c.tool_call("Bash", f"cmd {t} " + "X" * 900, f"output {t} " * 150)
        c.human(f"turn {t}")
        msgs = optimize(c.body())["messages"]
        for p in range(len(msgs)):
            form = ser_content(msgs[p])
            seq = distinct.setdefault(p, [])
            if not seq or seq[-1] != form:
                seq.append(form)
    thrashing = {p: len(s) for p, s in distinct.items() if len(s) > 2}
    assert not thrashing, (
        f"positions changed content >2x (cache thrash): {dict(list(thrashing.items())[:8])}")
    # and confirm collapses actually happened (>=1 position made the 2-form transition)
    assert any(len(s) == 2 for s in distinct.values()), "no collapse transition observed"


def test_deep_history_byte_frozen():
    """Beyond the recent flux window, the optimized content prefix must be
    byte-identical between consecutive turns (cache_control stripped)."""
    FLUX = 3 * KEEP + 6  # ~messages within the active keep window + margin
    c = Conversation().human("start")
    prev = None
    violations = []
    for t in range(22):
        c.tool_call("Bash", f"run {t} " + "Z" * 900, f"res {t} " * 150)
        c.human(f"t{t}")
        msgs = optimize(c.body())["messages"]
        deep_end = max(0, len(msgs) - FLUX)
        deep = [ser_content(m) for m in msgs[:deep_end]]
        if prev is not None:
            # every deep message present last turn must be unchanged this turn
            for p in range(min(len(prev), deep_end)):
                if prev[p] != deep[p]:
                    violations.append((t, p))
        prev = [ser_content(m) for m in msgs]  # full, so next turn can index back
    assert not violations, f"deep-history content changed: {violations[:8]}"


def test_cache_breakpoint_moves_only_forward():
    """The injected cache_control breakpoint must never jump backward into the
    already-cached zone (which would invalidate it); it should advance or hold."""
    c = Conversation().human("start")
    positions = []
    for t in range(20):
        c.tool_call("Bash", f"c {t} " + "Y" * 900, f"r {t} " * 150)
        c.human(f"t{t}")
        msgs = optimize(c.body())["messages"]
        bp = [p for p, m in enumerate(msgs)
              if any(isinstance(b, dict) and "cache_control" in b
                     for b in (m.get("content") or []) if isinstance(b, dict))]
        # track the earliest breakpoint we inject (CC also has its own recent ones)
        positions.append(min(bp) if bp else None)
    seen = [p for p in positions if p is not None]
    backward = [(i, seen[i - 1], seen[i]) for i in range(1, len(seen)) if seen[i] < seen[i - 1]]
    assert not backward, f"cache breakpoint moved backward (invalidates cache): {backward[:5]}"


def test_idempotent():
    """Optimizing an already-optimized body removes nothing more and is stable."""
    c = Conversation().human("start")
    for i in range(10):
        c.tool_call("Bash", "echo " + "C" * 900, "r" * 1000)
        c.human(f"q{i}")
    once = optimize(c.body())
    twice = optimize(once)
    assert ser(once["messages"]) == ser(twice["messages"]), "second optimization pass changed the body"


def test_roundtrip_recoverable():
    """Every collapsed pair leaves a hash that resolves to the original
    {tool_use, tool_result} in vt_object_store — nothing is destroyed."""
    vt_object_store.clear()
    c = Conversation().human("start")
    originals = []
    for i in range(8):
        cmd = f"unique-command-{i} " + "D" * 900
        res = f"unique-result-{i} " + "E" * 800
        c.tool_call("Bash", cmd, res)
        originals.append((cmd, res))
        c.human(f"q{i}")
    body = optimize(c.body())
    # collect hashes from collapsed text blocks
    import re
    hashes = []
    for _, _, _, b in iter_blocks(body):
        if b.get("type") == "text":
            m = re.search(r'get_vt_object\(hash="([0-9a-f]+)"\)', b.get("text", ""))
            if m:
                hashes.append(m.group(1))
    assert hashes, "no collapsed-pair references found"
    recovered_cmds = set()
    for h in set(hashes):
        obj = vt_object_store.get(h)
        assert obj and obj["type"] == "tool_pair", f"hash {h} missing/wrong type"
        tu = obj["data"]["tool_use"]
        recovered_cmds.add(tu["input"]["command"])
    for cmd, _ in originals[: -KEEP - 1]:  # the settled ones
        assert cmd in recovered_cmds, f"original call not recoverable: {cmd[:30]}"


def test_recent_calls_preserved():
    """Calls within keep_turns must stay intact (the model still needs them)."""
    c = Conversation().human("start")
    for i in range(3):  # fewer than KEEP turns -> nothing should collapse
        c.tool_call("Bash", "echo " + "F" * 900, "out")
        c.human(f"q{i}")
    body = optimize(c.body())
    # the most recent tool_use must still be a real tool_use with its command
    last_tu = [b for _, _, _, b in iter_blocks(body) if b.get("type") == "tool_use"]
    assert last_tu, "recent tool_use was wrongly collapsed"
    assert any("command" in (b.get("input") or {}) for b in last_tu), "recent call lost its args"


def test_human_text_untouched():
    """Human-typed messages are never altered by optimization."""
    c = Conversation()
    human_texts = []
    for i in range(12):
        t = f"please do task number {i} carefully"
        c.human(t)
        human_texts.append(t)
        c.tool_call("Bash", "echo " + "G" * 900, "r" * 600)
    body = optimize(c.body())
    seen = {b["text"] for _, _, role, b in iter_blocks(body)
            if role == "user" and b.get("type") == "text"}
    for t in human_texts:
        assert t in seen, f"human message altered or lost: {t!r}"


def test_message_count_preserved():
    """Collapsing converts blocks in place; it must not drop or add messages."""
    c = Conversation().human("start")
    for i in range(10):
        c.tool_call("Bash", "echo " + "H" * 900, "r" * 500)
        c.human(f"q{i}")
    raw = c.body()
    body = optimize(raw)
    assert len(body["messages"]) == len(raw["messages"]), "message count changed"
    for m in body["messages"]:
        c2 = m.get("content")
        assert not (isinstance(c2, list) and len(c2) == 0), "empty content array produced (API would reject)"


def test_parallel_tool_calls_paired_correctly():
    """Multiple tool calls in one assistant message, each with its own result:
    collapse must pair by id and never cross-wire or orphan."""
    c = Conversation().human("start")
    for i in range(8):
        c.tool_call("Bash", "echo " + "P" * 900, "r1" * 400,
                    parallel=[("Bash", "echo " + "Q" * 900, "r2" * 400)])
        c.human(f"q{i}")
    body = optimize(c.body())
    tu, tr = tool_use_ids(body), tool_result_ids(body)
    assert not (tr - tu), f"parallel-call collapse orphaned results: {tr - tu}"


def test_tool_results_lead_their_turn():
    """Anthropic requires every tool_result to be the FIRST block(s) of its user
    turn. A mixed parallel turn — one long call (collapses to text) beside a short
    call (stays a tool_result) — must not leave the text ref ahead of the surviving
    tool_result. This is the production 400 ('tool_results must lead the turn')."""
    c = Conversation().human("start")
    for i in range(8):
        # long call (will collapse -> text) + short sibling (stays tool_result)
        c.tool_call("Bash", "echo " + "L" * 900, "big " * 300,
                    parallel=[("Bash", "ls", "tiny output")])
        c.human(f"q{i}")
    body = optimize(c.body())
    for mi, m in enumerate(body["messages"]):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        seen_non_tr = False
        for b in content:
            is_tr = isinstance(b, dict) and b.get("type") == "tool_result"
            if is_tr:
                assert not seen_non_tr, (
                    f"msg[{mi}]: tool_result preceded by a non-tool_result block "
                    f"(the production 400)")
            else:
                seen_non_tr = True


def test_short_calls_left_inline():
    """Short tool calls (< MIN_CHARS) are NOT collapsed even when old."""
    c = Conversation().human("start")
    for i in range(12):
        c.tool_call("Bash", "ls -la", "short output")  # tiny call
        c.human(f"q{i}")
    body = optimize(c.body())
    # every Bash call should still be a real tool_use with its command intact
    bash_calls = [b for _, _, _, b in iter_blocks(body)
                  if b.get("type") == "tool_use" and b.get("name") == "Bash"]
    assert len(bash_calls) == 12, f"short calls were collapsed: {len(bash_calls)}/12 survive"
    assert all("command" in (b.get("input") or {}) for b in bash_calls)


def test_file_tools_never_collapsed():
    """Read/Write/Edit are handled by dedup, not here — never collapse them."""
    c = Conversation().human("start")
    for i in range(10):
        c.tool_call("Edit", {"old_string": "X" * 900, "new_string": "Y" * 900}, "edited ok")
        c.human(f"q{i}")
    body = optimize(c.body())
    edits = [b for _, _, _, b in iter_blocks(body)
             if b.get("type") == "tool_use" and b.get("name") == "Edit"]
    assert len(edits) == 10, f"Edit calls were wrongly collapsed: {len(edits)}/10 survive"


# ───────────────────────────────── runner ─────────────────────────────────
def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            vt_object_store.clear()  # isolation
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
