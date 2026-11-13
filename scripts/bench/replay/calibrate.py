"""Compare simulated with billed usage: python sim.py off T.jsonl | python calibrate.py [-v]"""
import json,sys
for l in sys.stdin:
    d=json.loads(l); R=d['rows']
    s=lambda k: sum((r[k] or 0) for r in R)
    print(f"{d['session']} {d['mode']:4} n={len(R)} sim read={s('read'):9.0f} write={s('w5')+s('w1'):8.0f} inp={s('inp'):6.0f} | actual read={s('a_read'):8} write={s('a_write'):7} inp={s('a_inp')}")
    if '-v' in sys.argv:
        for r in R: print(f"   sim r={r['read']:8.0f} w={r['w5']+r['w1']:7.0f}  act r={r['a_read']} w={r['a_write']}")
