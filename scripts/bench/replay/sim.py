"""Replay a Claude Code session's API requests through the optimizer pipeline
and a model of Anthropic's prompt cache. Makes no API calls.

    VOITTA_DESKTOP_HOME=<scratch dir> python sim.py MODE TRANSCRIPT.jsonl [...]

Set VOITTA_DESKTOP_HOME to a scratch directory: the optimizers write what they
strip to the object store under it.

MODE:
  off      the requests as Claude Code sent them
  on       the current pipeline (get_vt_object assumed available)
  on+bp    as on, but the pipeline's own cache breakpoint is always injected
  stepK    as on, but the strip threshold advances K typed turns at a time
  cold     as on, but the strip threshold moves only on a request whose cache
           is cold anyway: the session's first, the first after a gap longer
           than the TTL, or the first after compaction. In between, the same
           messages stay stripped, so no cached prefix is rewritten (#5,
           option b). BashCompressor still runs on every request.

The cache model: blocks in tools -> system -> messages order, an entry per
cache_control breakpoint, a lookup that walks back up to 20 blocks from each
breakpoint, entries that expire after their TTL unless read. Tokens are
estimated at CPT characters per token (default 2.5, fitted against billed
usage; check a new session with calibrate.py). Prints one JSON line per
transcript with per-request input, cache read and cache write, plus billed
usage where the transcript has it.
"""
import asyncio, copy, hashlib, json, os, sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(__file__))
from rebuild import requests  # noqa: E402

LOOKBACK = 20
TTL_S = {"5m": 300, "1h": 3600}
WRITE_MULT = {"5m": 1.25, "1h": 2.0}
READ_MULT = 0.1
CHARS_PER_TOKEN = float(os.environ.get("CPT", "2.5"))


def tok(obj) -> int:
    if isinstance(obj, dict):
        t = obj.get("type")
        if t == "text":
            return len(obj.get("text", "")) / CHARS_PER_TOKEN
        if t == "image":
            from optimizers import image_tokens
            return image_tokens(obj)
        if t == "tool_result" and isinstance(obj.get("content"), list):
            return 10 + sum(tok(b) for b in obj["content"])
        if t == "thinking":
            return len(obj.get("thinking", "")) / CHARS_PER_TOKEN
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return len(s) / CHARS_PER_TOKEN


def strip_cc(b):
    if isinstance(b, dict) and "cache_control" in b:
        b = {k: v for k, v in b.items() if k != "cache_control"}
    return b


def blocks_of(body):
    """[(cumulative hash, tokens, ttl or None)] in cache order."""
    out, hsh = [], hashlib.sha256()
    def add(b, extra_tokens=0):
        cc = b.get("cache_control") if isinstance(b, dict) else None
        hsh.update(json.dumps(strip_cc(b), sort_keys=True, separators=(",", ":")).encode())
        out.append((hsh.copy().hexdigest(), tok(strip_cc(b)) + extra_tokens,
                    (cc or {}).get("ttl", "5m") if cc else None))
    for t in body.get("tools") or []:
        add(t)
    for s in body.get("system") or []:
        add(s)
    for m in body["messages"]:
        hsh.update(m["role"].encode())
        c = m["content"]
        if isinstance(c, str):
            add({"type": "text", "text": c}, 4)
        else:
            for i, b in enumerate(c):
                add(b, 4 if i == 0 else 0)
    return out


class Cache:
    def __init__(self):
        self.entries = {}  # prefix hash -> expiry

    def request(self, blocks, now):
        bps = [i for i, b in enumerate(blocks) if b[2]]
        hit = -1
        for bp in bps:
            for j in range(bp, max(-1, bp - LOOKBACK - 1), -1):
                e = self.entries.get(blocks[j][0])
                if e and e[0] >= now:
                    hit = max(hit, j)
                    break
        last = bps[-1] if bps else -1
        cum = [0.0]
        for b in blocks:
            cum.append(cum[-1] + b[1])
        read = cum[hit + 1]
        # Writes are priced by the TTL of the breakpoint that covers them.
        write = {"5m": 0.0, "1h": 0.0}
        start = hit + 1
        for bp in bps:
            if bp >= start:
                write[blocks[bp][2]] += cum[bp + 1] - cum[start]
                start = bp + 1
        inp = cum[-1] - cum[max(last, hit) + 1]
        if hit >= 0:
            ttl = self.entries[blocks[hit][0]][1]
            self.entries[blocks[hit][0]] = (now + TTL_S[ttl], ttl)
        for bp in bps:
            ttl = blocks[bp][2]
            self.entries[blocks[bp][0]] = (now + TTL_S[ttl], ttl)
        return inp, read, write


def set_cc(body, cache_control):
    for c in cache_control:
        p, v = c["path"], c["value"]
        if p[0] == "system":
            body["system"][p[1]] = dict(body["system"][p[1]], cache_control=v)
        elif p[0] == "messages":
            m = body["messages"][p[1]]
            content = m["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            content = list(content)
            content[p[3]] = dict(content[p[3]], cache_control=v)
            body["messages"][p[1]] = dict(m, content=content)


def build_pipeline(mode):
    from optimizers import BaseOptimizer, OptimizerPipeline
    from optimizers.bash_compress import BashCompressor
    from optimizers.image import ImageOptimizer
    from optimizers.thinking import ThinkingOptimizer
    from optimizers.tool_result import ToolResultOptimizer
    from optimizers.tool_use import ToolUseOptimizer
    if mode.startswith("step"):
        k = int(mode[4:].split("+")[0])
        orig = BaseOptimizer._threshold_msg_index
        def stepped(self, messages):
            starts = self._find_user_turn_starts(messages)
            n = len(starts) - self.keep_turns  # turns eligible today
            n = (n // k) * k
            if n <= 0:
                return None
            return starts[n]
        BaseOptimizer._threshold_msg_index = stepped
    if mode.startswith("cold"):
        frozen = {}
        def frozen_threshold(self, messages):
            starts = self._find_user_turn_starts(messages)
            n = len(starts) - self.keep_turns
            if COLD["now"] or id(self) not in frozen:
                frozen[id(self)] = max(0, n)
            n = min(frozen[id(self)], n)
            return starts[n] if n > 0 else None
        BaseOptimizer._threshold_msg_index = frozen_threshold
    pipe = OptimizerPipeline([BashCompressor(), ToolUseOptimizer(), ToolResultOptimizer(),
                              ImageOptimizer(), ThinkingOptimizer()], enabled=mode != "off")
    if "+bp" in mode:
        pipe.CACHE_BP_API_MAX = 99
    return pipe


COLD = {"now": False}  # set per request by run(); read by the cold threshold

VT_TOOL = {"name": "mcp__voitta__get_vt_object", "description": "x",
           "input_schema": {"type": "object"}}


def run(mode, path):
    import logging
    logging.disable(logging.CRITICAL)
    from middleware.base import ProxyRequest
    lines, reqs = requests(path)
    shapes = {e["shapeHash"]: e["shape"] for e in lines if e.get("type") == "api-request-shape"}
    actual = {}
    for e in lines:
        if e.get("type") == "assistant" and e.get("requestRef"):
            actual.setdefault(e["requestRef"], e["message"].get("usage") or {})
    pipe = build_pipeline(mode)
    cache = Cache()
    warm = True
    rows = []
    prev_ts, prev_first = None, None
    for r in reqs:
        if any(m is None for m in r["messages"]) or r["shape"] not in shapes:
            continue
        shape = shapes[r["shape"]]
        body = {"model": r["model"], "tools": copy.deepcopy(shape.get("tools") or []),
                "system": copy.deepcopy(shape.get("system") or []),
                "messages": copy.deepcopy(r["messages"])}
        set_cc(body, r["cache_control"])
        body["tools"].append(VT_TOOL)  # treat the session as able to resolve refs
        now = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
        ttl = max((TTL_S[c["value"].get("ttl", "5m")] for c in r["cache_control"]), default=300)
        first = json.dumps(r["messages"][0], sort_keys=True) if r["messages"] else None
        COLD["now"] = prev_ts is None or now - prev_ts > ttl or first != prev_first
        prev_ts, prev_first = now, first
        req = ProxyRequest(method="POST", path="/v1/messages", headers={}, body=b"")
        req.json = body
        req = asyncio.run(pipe.on_request(req))
        body = req.json
        body["tools"] = [t for t in body["tools"] if t is not VT_TOOL and t.get("name") != VT_TOOL["name"]]
        blocks = blocks_of(body)
        if warm:
            # Other sessions keep tools + system cached; start with them warm.
            n_fixed = len(body["tools"]) + len(body["system"])
            for b in blocks[:n_fixed]:
                if b[2]:
                    cache.entries[b[0]] = (now + TTL_S[b[2]], b[2])
            warm = False
        inp, read, write = cache.request(blocks, now)
        a = actual.get(r["id"], {})
        rows.append({"ts": r["ts"], "inp": inp, "read": read, "w5": write["5m"], "w1": write["1h"],
                     "a_inp": a.get("input_tokens"), "a_read": a.get("cache_read_input_tokens"),
                     "a_write": a.get("cache_creation_input_tokens"), "cold": COLD["now"]})
    claimed = sum(sum(o.tokens_saved.values()) for o in pipe.optimizers)
    return rows, claimed


def units(r):
    return r["inp"] + r["read"] * READ_MULT + r["w5"] * WRITE_MULT["5m"] + r["w1"] * WRITE_MULT["1h"]


if __name__ == "__main__":
    mode = sys.argv[1]
    for p in sys.argv[2:]:
        rows, claimed = run(mode, p)
        print(json.dumps({"session": os.path.basename(p)[:8], "mode": mode, "claimed": claimed, "rows": rows}))
