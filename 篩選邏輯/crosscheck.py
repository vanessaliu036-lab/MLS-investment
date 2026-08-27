import sqlite3, json
import pullback_discovery as pd

conn = sqlite3.connect('mls.db')

events = pd.find_limitup_events('mls.db')
print(f"events found: {len(events)}")

def rows(code, d1_date):
    c = sqlite3.connect('mls.db'); c.row_factory = sqlite3.Row
    r = c.execute("SELECT slot,price,volume,net_active FROM b_snapshot WHERE code=? AND data_date=? ORDER BY slot", (code, d1_date)).fetchall()
    c.close()
    return [dict(x) for x in r]

cases = []
for ev in events:
    slots = rows(ev['code'], ev['d1_date'])
    if len(slots) < 10:
        continue
    case = pd.compute_case(ev, slots)
    if case is not None:
        cases.append(case)

from collections import Counter
print(Counter(c['classification'] for c in cases))

old = {(r['code'], r['d1_date']): r for r in json.load(open('/tmp/pullback_results_v2.json'))}
mismatches = 0
checked = 0
for c in cases:
    key = (c['code'], c['d1_date'])
    if key not in old:
        continue
    o = old[key]
    checked += 1
    for field in ['classification','peak_idx','trough_idx','reclaim_idx','pullback_depth','flow_retention','volume_contraction','support_hold','net_h30m','net_close','mae_h60m']:
        a, b = c.get(field), o.get(field)
        if a is None and b is None: continue
        if isinstance(a, float) or isinstance(b, float):
            if a is None or b is None or abs(a-b) > 1e-9:
                print(f"MISMATCH {key} {field}: new={a} old={b}")
                mismatches += 1
        else:
            if a != b:
                print(f"MISMATCH {key} {field}: new={a} old={b}")
                mismatches += 1
print(f"checked={checked} mismatches={mismatches}")
