"""Run several modes over sessions; print input-side cost in units of
base input tokens (input 1x, cache read 0.1x, 5m write 1.25x, 1h write 2x),
the change against the first mode, and what the app would have claimed for
"on" (tokens removed priced as cache reads).

    VOITTA_DESKTOP_HOME=<scratch dir> python compare.py off,on,step5 T.jsonl [...]
"""
import json, os, subprocess, sys
modes = sys.argv[1].split(",")
here = os.path.dirname(os.path.abspath(__file__))
from sim import units, READ_MULT
print(f"{'session':9}{'reqs':>5}" + "".join(f"{m:>16}" for m in modes) + "   claimed(on)")
for p in sys.argv[2:]:
    res = {}
    for m in modes:
        out = subprocess.run([sys.executable, os.path.join(here, "sim.py"), m, p], capture_output=True, text=True, check=True).stdout
        d = json.loads(out)
        res[m] = (sum(units(r) for r in d["rows"]), d["claimed"], len(d["rows"]))
    base = res[modes[0]][0]
    cells = "".join(f"{res[m][0]:>9.0f}{(res[m][0]-base)/base*100:>+6.0f}% " for m in modes)
    on = res.get("on")
    print(f"{os.path.basename(p)[:8]:9}{res[modes[0]][2]:>5}{cells}   {on[1]*READ_MULT if on else 0:>8.0f}")
