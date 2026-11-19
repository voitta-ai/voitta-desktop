"""Rebuild the API requests Claude Code sent, from its session transcript.

Claude Code (2.1.323 here) logs each request as an ``api-request`` entry: the
number of messages kept from the previous request, hashes of the messages
appended, and where the cache_control breakpoints went. Messages that are
not otherwise in the transcript are stored as ``api-request-blob`` entries;
the rest are found by hashing the transcript's own user/assistant messages.

    python rebuild.py TRANSCRIPT.jsonl [...]   # prints coverage
"""
import hashlib, json, sys
from collections import OrderedDict

def h(msg, ascii_):
    s = json.dumps({"role": msg["role"], "content": msg["content"]},
                   separators=(",", ":"), sort_keys=True, ensure_ascii=ascii_)
    return hashlib.sha256(s.encode()).hexdigest()

def index(lines):
    idx = {}
    def add(m):
        for a in (False, True):
            idx.setdefault(h(m, a), m)
        if isinstance(m["content"], str):
            add({"role": m["role"], "content": [{"type": "text", "text": m["content"]}]})
    by_id = OrderedDict()
    for e in lines:
        t = e.get("type")
        if t == "api-request-blob":
            idx[e["hash"]] = e["message"]
        elif t in ("user", "assistant") and isinstance(e.get("message"), dict):
            m = e["message"]
            add({"role": m["role"], "content": m["content"]})
            if t == "assistant" and m.get("id"):
                by_id.setdefault(m["id"], []).extend(m["content"])
    for content in by_id.values():
        add({"role": "assistant", "content": content})
    # Parallel tool calls: each tool_result is its own transcript entry, but
    # the API message carries them together.
    run = []
    for e in lines:
        t = e.get("type")
        if t == "user" and isinstance(e.get("message"), dict) and isinstance(e["message"]["content"], list):
            run.extend(e["message"]["content"])
            if len(run) > len(e["message"]["content"]):
                add({"role": "user", "content": list(run)})
        elif t == "assistant":
            run = []
    return idx

def requests(path):
    lines = [json.loads(l) for l in open(path)]
    idx = index(lines)
    usage = {}
    for e in lines:
        if e.get("type") == "assistant" and e.get("requestId"):
            usage[e["requestId"]] = e["message"].get("usage")
    results = {}
    for e in lines:
        if e.get("type") == "user" and isinstance(e.get("message"), dict) and isinstance(e["message"]["content"], list):
            for b in e["message"]["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = b
    prev, out = [], []
    for e in lines:
        if e.get("type") != "api-request":
            continue
        msgs = prev[:e["keep"]]
        for x in e["tail"]:
            m = idx.get(x)
            if m is None and msgs and msgs[-1] and msgs[-1]["role"] == "assistant":
                # Content differs from the transcript copy (e.g. an image
                # result); rebuild it from the transcript's tool results.
                ids = [b["id"] for b in msgs[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
                if ids and all(i in results for i in ids):
                    m = {"role": "user", "content": [results[i] for i in ids]}
            msgs.append(m)
        prev = msgs
        out.append({"id": e["id"], "ts": e["timestamp"], "model": e["params"].get("model"),
                    "source": e.get("querySource"), "shape": e.get("shapeHash"),
                    "messages": msgs, "cache_control": e.get("cacheControl", [])})
    return lines, out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        _, reqs = requests(p)
        miss = sum(m is None for r in reqs for m in r["messages"])
        tot = sum(len(r["messages"]) for r in reqs)
        print(p.split("/")[-1][:8], "requests", len(reqs), "msgs", tot, "missing", miss)
