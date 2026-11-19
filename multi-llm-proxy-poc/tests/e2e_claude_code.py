#!/usr/bin/env python3
"""Drive the real Claude Code CLI through the gateway against the fake upstream.

For each translated adapter (Chat Completions and Codex Responses) Claude Code
must: send its request through the gateway, get a Bash tool call back, run
it, send the result, and print the fake model's final answer.

    python tests/e2e_claude_code.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_PORT, GW_PORT = 18999, 18950


def wait_for(url):
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"{url} did not come up")


def api(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{GW_PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def main():
    home = Path(tempfile.mkdtemp(prefix="llmgw-e2e-"))
    fake_url = f"http://127.0.0.1:{FAKE_PORT}"
    far = time.time() + 86400
    (home / "accounts.json").write_text(json.dumps({"active": "mistral:fake", "accounts": {
        "mistral:fake": {"id": "mistral:fake", "provider": "mistral", "kind": "openai_chat", "label": "fake mistral",
                         "base_url": f"{fake_url}/v1", "models": {"big": "mistral-large-latest", "small": "mistral-small-latest"},
                         "options": {}, "credentials": {"api_key": "sk-fake"}, "info": {}, "status": "ok",
                         "status_detail": "", "created_at": 0, "updated_at": 0},
        "openai:fake": {"id": "openai:fake", "provider": "openai", "kind": "openai_codex", "label": "fake chatgpt",
                        "base_url": f"{fake_url}/codex", "models": {"big": "gpt-5.5", "small": "gpt-5.5"},
                        "options": {}, "credentials": {"access_token": "t", "refresh_token": "r", "account_id": "a",
                                                       "expires_at": far},
                        "info": {}, "status": "ok", "status_detail": "", "created_at": 0, "updated_at": 0},
    }}))
    env = {**os.environ, "LLMGW_HOME": str(home), "LLMGW_PORT": str(GW_PORT), "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen([sys.executable, "tests/fake_upstream.py", str(FAKE_PORT)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                         stdout=open(home / "gateway.log", "w"), stderr=subprocess.STDOUT),
    ]
    failures = 0
    try:
        wait_for(f"http://127.0.0.1:{GW_PORT}/_gw/api/state")
        # Claude Code keeps its own login (the gateway ignores it for these accounts).
        # No ANTHROPIC_AUTH_TOKEN and no CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:
        # either one saves Claude Code's mods switch as off for every session on
        # the machine until a normal run refreshes it.
        gw_env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{GW_PORT}"}
        claude_env = {**os.environ, **gw_env}
        claude_env.pop("ANTHROPIC_API_KEY", None)
        claude_env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
        # ~/.claude/settings.json may set ANTHROPIC_BASE_URL itself (Voitta Desktop
        # does); --settings outranks it, the process env does not.
        settings = json.dumps({"env": gw_env})
        for account in ("mistral:fake", "openai:fake"):
            api("POST", f"/_gw/api/accounts/{account}/activate")
            out = subprocess.run(
                ["claude", "-p", "Run the marker command and report its output.",
                 "--allowedTools", "Bash(echo:*)", "--strict-mcp-config", "--settings", settings, "--model", "claude-sonnet-4-5"],
                env=claude_env, capture_output=True, text=True, timeout=180, cwd=home)
            ok = "Final answer: voitta-e2e-ok" in out.stdout
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {account}: exit={out.returncode} stdout={out.stdout.strip()[:300]!r}")
            if not ok:
                print("  stderr:", out.stderr.strip()[:1500])
        # Per-window routing relies on every request carrying the session id:
        # the main loop, its subagents, and the same id again after --resume.
        api("POST", "/_gw/api/accounts/mistral:fake/activate")
        sid = str(uuid.uuid4())

        def run(*args):
            seen = {id(r) for r in []}
            before = {(r["ts"], r["path"]) for r in api("GET", "/_gw/api/state")["requests"]}
            out = subprocess.run(["claude", "-p", *args, "--allowedTools", "Bash(echo:*)", "Agent", "Task",
                                  "--strict-mcp-config", "--settings", settings, "--model", "claude-sonnet-4-5"],
                                 env=claude_env, capture_output=True, text=True, timeout=240, cwd=home)
            new = [r for r in api("GET", "/_gw/api/state")["requests"] if (r["ts"], r["path"]) not in before]
            return out, new

        out, new = run("SUBAGENT: hand the marker command to a subagent.", "--session-id", sid)
        sessions = {r.get("session") for r in new}
        ok = "voitta-e2e-ok" in out.stdout and sessions == {sid[:8]} and len(new) >= 4
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] subagent requests carry the session id: "
              f"{len(new)} requests, sessions={sessions}, stdout={out.stdout.strip()[:160]!r}")
        out, new = run("--resume", sid, "Run the marker command and report its output.")
        sessions = {r.get("session") for r in new}
        print(f"[INFO] after --resume: sessions={sessions} (original {sid[:8]})")

        # The /llm mod: one window picks ChatGPT, another stays on the default (Mistral).
        mod = str(ROOT / "mod" / "voitta-llm")
        picked_sid, other_sid = str(uuid.uuid4()), str(uuid.uuid4())
        out, _ = run("/llm chatgpt", "--session-id", picked_sid, "--plugin-dir", mod)
        ok = "This window now uses fake chatgpt" in out.stdout
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] /llm chatgpt: stdout={out.stdout.strip()[:200]!r} stderr={out.stderr.strip()[-300:]!r}")
        out, new = run("--resume", picked_sid, "Run the marker command and report its output.", "--plugin-dir", mod)
        routes = {(r.get("account"), r.get("route")) for r in new}
        ok = "voitta-e2e-ok" in out.stdout and routes == {("fake chatgpt", "window")}
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] picked window goes to its account: {routes}")
        out, new = run("Run the marker command and report its output.", "--session-id", other_sid, "--plugin-dir", mod)
        routes = {(r.get("account"), r.get("route")) for r in new}
        ok = "voitta-e2e-ok" in out.stdout and routes == {("fake mistral", "default")}
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] other window stays on the default: {routes}")

        for r in api("GET", "/_gw/api/state")["requests"][::-1]:
            print(f"  [{r.get('session')}] {r.get('account')}: {r.get('model')} -> {r.get('upstream_model')} "
                  f"status={r['status']} stream={r.get('stream')} err={r.get('error', '')[:120]}")
    finally:
        for p in procs:
            p.terminate()
    if failures:
        print("gateway log:", home / "gateway.log")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
