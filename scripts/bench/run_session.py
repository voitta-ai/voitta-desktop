"""Run one benchmark session of Claude Code through the Voitta Desktop proxies.

A task file (JSON) names the repo to work in, the model, the tools and the
prompts. The prompts go to a single ``claude -p --input-format stream-json``
process one at a time, each after the previous result, so they stay separate
human turns (the reference optimizers only touch content older than the last
few of those) and the session opens one MCP connection, i.e. one tool-gate
popup.

Results go to ``--out-dir`` (default ``~/.voitta-desktop/bench/<task name>/``),
never into this repo: ``<label>.jsonl`` holds every stream event, including
the full text of whatever the model read. Keep it private when the target
repo is.

Toggle the optimizer in the app before a run. The run records the value of
``optimizer.enabled`` it found in apps.json.

    python scripts/bench/run_session.py TASK.json on-1
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from paths import CONFIG_PATH, ROOT  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", type=Path)
    ap.add_argument("label", help="run name, e.g. on-1; summarize.py groups by the part before '-'")
    ap.add_argument("--out-dir", type=Path)
    args = ap.parse_args()

    task = json.loads(args.task.read_text())
    out_dir = args.out_dir or ROOT / "bench" / task["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / f"{args.label}.jsonl"
    if events_path.exists():
        sys.exit(f"{events_path} exists; pick another label")

    cfg = json.loads(CONFIG_PATH.read_text())
    llm_port = cfg["llm_proxy"]["port"]
    mcp_port = cfg["mcp_proxy"]["port"]

    # --settings outranks ~/.claude/settings.json, whose env may pin
    # ANTHROPIC_BASE_URL elsewhere and would override the process env.
    settings_path = out_dir / "settings.json"
    settings_path.write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{llm_port}",
        "ENABLE_TOOL_SEARCH": "true",
    }}))
    mcp_path = out_dir / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"voitta": {
        "type": "http", "url": f"http://127.0.0.1:{mcp_port}/mcp",
    }}}))

    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--model", task.get("model", "sonnet"),
           "--settings", str(settings_path),
           "--mcp-config", str(mcp_path), "--strict-mcp-config",
           "--allowedTools", ",".join(task["allowed_tools"] + ["mcp__voitta__get_vt_object"])]
    if task.get("disallowed_tools"):
        cmd += ["--disallowedTools", ",".join(task["disallowed_tools"])]

    meta = {"label": args.label, "task": task["name"], "model": task.get("model", "sonnet"),
            "optimizer_enabled": cfg.get("optimizer", {}).get("enabled", True),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    print(json.dumps(meta), flush=True)

    repo = Path(task["repo"]).expanduser()
    with events_path.open("w") as out:
        out.write(json.dumps({"type": "bench_meta", **meta}) + "\n")
        proc = subprocess.Popen(cmd, cwd=repo, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        for i, prompt in enumerate(task["prompts"], 1):
            proc.stdin.write(json.dumps(
                {"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            proc.stdin.flush()
            started = time.time()
            for line in proc.stdout:
                out.write(line)
                out.flush()
                event = json.loads(line)
                if event.get("type") == "result":
                    u = event.get("usage") or {}
                    print(f"{args.label} prompt {i}: {time.time() - started:.0f}s "
                          f"cost=${event.get('total_cost_usd')} in={u.get('input_tokens')} "
                          f"cache_read={u.get('cache_read_input_tokens')} "
                          f"cache_write={u.get('cache_creation_input_tokens')} "
                          f"out={u.get('output_tokens')}", flush=True)
                    break
            else:
                sys.exit(f"{args.label}: claude exited before prompt {i} finished")
        proc.stdin.close()
        proc.wait(timeout=60)
        # summarize.py matches desktop.log lines to this run by time window.
        out.write(json.dumps({"type": "bench_end",
                              "ended": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")


if __name__ == "__main__":
    main()
