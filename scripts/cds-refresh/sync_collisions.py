"""Sync COLLEGES fields to match CDS_VERIFIED for any contradiction.

CDS_VERIFIED is the authoritative source per the docstring at line 2585
('Highest authority for any field it specifies'). When COLLEGES drifts,
sync it back. This is purely internal-consistency cleanup — users already
see CDS_VERIFIED values at runtime.
"""
import re
from pathlib import Path

APP_PY = Path("/Users/jasperlasser/Desktop/college-tool/app.py")
text = APP_PY.read_text()

cb_start = text.find("COLLEGES = [")
cb_end = text.find("\nCOLLEGES_BY_SLUG")
COLLEGES = eval(text[cb_start:cb_end].split("=", 1)[1].strip().rstrip(","), {"__builtins__": {}}, {})

m = re.search(r"^CDS_VERIFIED = \{.*?^\}", text, re.DOTALL | re.MULTILINE)
CDS_VERIFIED = eval(m.group().split("=", 1)[1], {"__builtins__": {}}, {})

FIELDS = ("accept", "sat_25", "sat_75", "act_25", "act_75")
TOL = {"accept": 0.005, "sat_25": 10, "sat_75": 10, "act_25": 2, "act_75": 2}

# Build per-slug patch list
patches: dict[str, dict] = {}
for c in COLLEGES:
    slug = c["slug"]
    cds = CDS_VERIFIED.get(slug)
    if not cds: continue
    for k in FIELDS:
        if k in cds and cds[k] is not None and c.get(k) is not None:
            if abs(c[k] - cds[k]) > TOL[k]:
                patches.setdefault(slug, {})[k] = cds[k]

print(f"sync plan: {len(patches)} schools have at least one drifted field")
field_counts = {k: 0 for k in FIELDS}
for p in patches.values():
    for k in p:
        field_counts[k] += 1
for k, n in field_counts.items():
    print(f"  {k:<10} {n}")

# Apply per-school by regex on the line
new_text = text
fixed = 0
for slug, updates in patches.items():
    pat = re.compile(r'(^.*"slug":"' + re.escape(slug) + r'".*$)', re.MULTILINE)
    sm = pat.search(new_text)
    if not sm:
        continue
    line = sm.group(1)
    for k, v in updates.items():
        field_pat = re.compile(r'("' + re.escape(k) + r'":)([0-9.]+)')
        if isinstance(v, float):
            new_str = f"{v:.4f}".rstrip("0").rstrip(".") if k == "accept" else f"{v}"
        else:
            new_str = str(int(v))
        line, n = field_pat.subn(lambda m_: m_.group(1) + new_str, line, count=1)
    new_text = new_text.replace(sm.group(1), line, 1)
    fixed += 1

APP_PY.write_text(new_text)
print(f"\nsynced {fixed} school lines in app.py")
