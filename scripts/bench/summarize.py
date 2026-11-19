"""Summarize benchmark runs written by run_session.py. Prints numbers only.

Per run: Claude Code's own billed usage (summed over the session's result
events, which report it per prompt), its cost (the last event's cumulative
total_cost_usd), and which optimizers logged savings in desktop.log during
the run's time window. Then the mean per group, where the group is the label
before the first '-' (on-1, on-2 -> on).

    python scripts/bench/summarize.py ~/.voitta-desktop/bench/<task name>
"""
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from paths import LOG_DIR  # noqa: E402

FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
SAVED_RE = re.compile(r"^(\S+ \S+),\d+ \[voitta-desktop\.optimizers\] INFO: (\w+): saved (\d+) tokens")


def saved_by_optimizer(start: str, end: str) -> Counter:
    """Tokens each optimizer reported saving between start and end (ISO, local time)."""
    start, end = start.replace("T", " "), end.replace("T", " ")
    saved: Counter = Counter()
    for log in sorted(LOG_DIR.glob("desktop.log*"), reverse=True):
        for line in log.read_text(errors="replace").splitlines():
            m = SAVED_RE.match(line)
            if m and start <= m.group(1) <= end:
                saved[m.group(2)] += int(m.group(3))
    return saved


def summarize_run(path: Path) -> dict:
    meta, end, results = {}, None, []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "bench_meta":
            meta = event
        elif event.get("type") == "bench_end":
            end = event["ended"]
        elif event.get("type") == "result":
            results.append(event)
    usage = {f: sum((r.get("usage") or {}).get(f, 0) for r in results) for f in FIELDS}
    return {
        "label": meta.get("label", path.stem),
        "optimizer": meta.get("optimizer_enabled"),
        "complete": end is not None,
        "prompts": len(results),
        "errors": sum(1 for r in results if r.get("is_error")),
        # A blocked tool call leaves the model without the content the task
        # depends on, so such a run doesn't measure the optimizer.
        "denied": sum(len(r.get("permission_denials") or []) for r in results),
        # total_cost_usd is cumulative over the session; usage is per prompt.
        "cost": (results[-1].get("total_cost_usd") or 0) if results else 0,
        **usage,
        "saved": saved_by_optimizer(meta["started"], end) if meta and end else Counter(),
    }


def main() -> None:
    runs = [summarize_run(p) for p in sorted(Path(sys.argv[1]).expanduser().glob("*.jsonl"))]
    head = f"{'run':<8}{'opt':<6}{'ok':<4}{'n':>3}{'deny':>5}{'input':>9}{'c_read':>11}{'c_write':>10}{'output':>8}{'cost $':>9}  saved"
    print(head)
    groups = defaultdict(list)
    for r in runs:
        groups[r["label"].split("-")[0]].append(r)
        saved = ", ".join(f"{k}={v}" for k, v in sorted(r["saved"].items())) or "-"
        print(f"{r['label']:<8}{str(r['optimizer']):<6}{'y' if r['complete'] and not r['errors'] and not r['denied'] else 'n':<4}"
              f"{r['prompts']:>3}{r['denied']:>5}{r['input_tokens']:>9}{r['cache_read_input_tokens']:>11}"
              f"{r['cache_creation_input_tokens']:>10}{r['output_tokens']:>8}"
              f"{r['cost']:>9.4f}  {saved}")
    print()
    for name, rs in sorted(groups.items()):
        costs = [r["cost"] for r in rs]
        spread = f" sd={statistics.stdev(costs):.4f}" if len(costs) > 1 else ""
        print(f"{name}: n={len(rs)} mean cost=${statistics.mean(costs):.4f}{spread}")


if __name__ == "__main__":
    main()
