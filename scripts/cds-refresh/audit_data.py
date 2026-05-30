"""Comprehensive data integrity audit for Candor.

Reads app.py and reports every contradiction, impossible value, or
inconsistency across COLLEGES, CDS_VERIFIED, ADMISSIONS_DETAIL.
"""
import re
import json
from pathlib import Path
from collections import Counter

APP = Path("/Users/jasperlasser/Desktop/college-tool/app.py").read_text()

def extract(start_pat: str, end_pat: str):
    s = APP.find(start_pat)
    e = APP.find(end_pat, s)
    return APP[s:e]

# Extract COLLEGES
cb = extract("COLLEGES = [", "\nCOLLEGES_BY_SLUG")
COLLEGES = eval(cb.split("=", 1)[1].strip().rstrip(","), {"__builtins__": {}}, {})

# Extract CDS_VERIFIED
m = re.search(r"^CDS_VERIFIED = \{.*?^\}", APP, re.DOTALL | re.MULTILINE)
CDS_VERIFIED = eval(m.group().split("=", 1)[1], {"__builtins__": {}}, {})

# Extract ADMISSIONS_DETAIL
ad_start = APP.find("ADMISSIONS_DETAIL = {")
ad_end = APP.find("\n}\n", ad_start)
ad_block = APP[ad_start:ad_end + 2]
ADMISSIONS_DETAIL = eval(ad_block.split("=", 1)[1], {"__builtins__": {}}, {})

print(f"COLLEGES:           {len(COLLEGES)} entries")
print(f"CDS_VERIFIED:       {len(CDS_VERIFIED)} entries")
print(f"ADMISSIONS_DETAIL:  {len(ADMISSIONS_DETAIL)} entries")
print()

issues = []

# ---- 1. Duplicate slugs in COLLEGES
slug_counts = Counter(c["slug"] for c in COLLEGES)
dups = [(s, n) for s, n in slug_counts.items() if n > 1]
if dups:
    issues.append(("duplicate_slug", dups))
print(f"[1] duplicate slugs in COLLEGES: {len(dups)}")
for s, n in dups[:10]:
    print(f"      {s} appears {n}x")

# ---- 2. CDS_VERIFIED slugs not in COLLEGES (orphan entries)
college_slugs = {c["slug"] for c in COLLEGES}
orphan_cds = sorted(set(CDS_VERIFIED.keys()) - college_slugs)
if orphan_cds:
    issues.append(("orphan_cds", orphan_cds))
print(f"\n[2] CDS_VERIFIED slugs NOT in COLLEGES: {len(orphan_cds)}")
for s in orphan_cds[:15]:
    print(f"      {s}")

# ---- 3. ADMISSIONS_DETAIL slugs not in COLLEGES
orphan_ad = sorted(set(ADMISSIONS_DETAIL.keys()) - college_slugs)
print(f"\n[3] ADMISSIONS_DETAIL slugs NOT in COLLEGES: {len(orphan_ad)}")
for s in orphan_ad[:15]:
    print(f"      {s}")

# ---- 4. Impossible SAT/ACT ranges (25 > 75)
impossible = []
for c in COLLEGES:
    if c.get("sat_25") and c.get("sat_75") and c["sat_25"] > c["sat_75"]:
        impossible.append((c["slug"], "sat", c["sat_25"], c["sat_75"]))
    if c.get("act_25") and c.get("act_75") and c["act_25"] > c["act_75"]:
        impossible.append((c["slug"], "act", c["act_25"], c["act_75"]))
    if c.get("gpa_lo") and c.get("gpa_hi") and c["gpa_lo"] > c["gpa_hi"]:
        impossible.append((c["slug"], "gpa", c["gpa_lo"], c["gpa_hi"]))
print(f"\n[4] impossible ranges (25 > 75): {len(impossible)}")
for slug, field, lo, hi in impossible[:10]:
    print(f"      {slug}: {field} {lo} > {hi}")

# ---- 5. Out-of-range values
oor = []
for c in COLLEGES:
    if c.get("accept") and not (0.005 <= c["accept"] <= 1.0):
        oor.append((c["slug"], "accept", c["accept"]))
    if c.get("sat_25") and not (400 <= c["sat_25"] <= 1600):
        oor.append((c["slug"], "sat_25", c["sat_25"]))
    if c.get("sat_75") and not (400 <= c["sat_75"] <= 1600):
        oor.append((c["slug"], "sat_75", c["sat_75"]))
    if c.get("act_25") and not (1 <= c["act_25"] <= 36):
        oor.append((c["slug"], "act_25", c["act_25"]))
    if c.get("act_75") and not (1 <= c["act_75"] <= 36):
        oor.append((c["slug"], "act_75", c["act_75"]))
    if c.get("gpa_hi") and c["gpa_hi"] > 5.0:
        oor.append((c["slug"], "gpa_hi", c["gpa_hi"]))  # weighted?
print(f"\n[5] out-of-range values: {len(oor)}")
for slug, field, v in oor[:15]:
    print(f"      {slug}: {field}={v}")

# ---- 6. CDS_VERIFIED contradicts COLLEGES (different sat/act values for same slug)
by_slug = {c["slug"]: c for c in COLLEGES}
contradictions = []
for slug, cds in CDS_VERIFIED.items():
    c = by_slug.get(slug)
    if not c: continue
    for k in ("accept", "sat_25", "sat_75", "act_25", "act_75"):
        if k in cds and cds[k] is not None and c.get(k) is not None:
            cv, cdsv = c[k], cds[k]
            # tolerance: 0.005 for accept, 10 for sat, 2 for act
            tol = 0.005 if k == "accept" else (10 if k.startswith("sat") else 2)
            if abs(cv - cdsv) > tol:
                contradictions.append((slug, k, cv, cdsv))
print(f"\n[6] CDS_VERIFIED contradicts COLLEGES beyond tolerance: {len(contradictions)}")
for slug, k, cv, cdsv in contradictions[:15]:
    print(f"      {slug}: {k} COLLEGES={cv}  CDS_VERIFIED={cdsv}")

# ---- 7. Missing required fields in COLLEGES
missing = []
required = ("name", "slug", "tier", "type", "state", "accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition", "desc", "majors")
for c in COLLEGES:
    for k in required:
        if c.get(k) is None or c.get(k) == "":
            missing.append((c["slug"], k))
print(f"\n[7] missing required fields: {len(missing)}")
for slug, k in missing[:15]:
    print(f"      {slug}: {k}")

# ---- 8. Schools with hardcoded $5/mo or other stale pricing strings in desc
stale_price = []
for c in COLLEGES:
    desc = c.get("desc") or ""
    if "$5/mo" in desc or "$5 per month" in desc or "subscribe" in desc.lower():
        stale_price.append(c["slug"])
print(f"\n[8] schools with stale pricing in desc: {len(stale_price)}")

# ---- 9. Tier vs accept rate sanity (tier 1 should be hardest)
tier_check = []
tiers = {1: [], 2: [], 3: [], 4: [], 5: []}
for c in COLLEGES:
    t = c.get("tier")
    a = c.get("accept")
    if t in tiers and a:
        tiers[t].append(a)
print(f"\n[9] tier accept-rate distribution (median):")
import statistics
for t in sorted(tiers):
    if tiers[t]:
        med = statistics.median(tiers[t])
        print(f"      tier {t}: {len(tiers[t]):3d} schools, median accept = {med*100:.1f}%")

# Find inversions: tier-1 with accept > 0.20 or tier-5 with accept < 0.30
for c in COLLEGES:
    if c.get("tier") == 1 and (c.get("accept") or 0) > 0.20:
        tier_check.append((c["slug"], 1, c["accept"]))
    if c.get("tier") == 5 and (c.get("accept") or 1) < 0.30:
        tier_check.append((c["slug"], 5, c["accept"]))
print(f"\n[9b] suspicious tier/accept pairs: {len(tier_check)}")
for slug, t, a in tier_check[:15]:
    print(f"      {slug}: tier {t} but accept {a*100:.1f}%")

# ---- 10. Names with typos / inconsistencies (rough heuristic: trailing space, double spaces)
weird_names = []
for c in COLLEGES:
    n = c["name"]
    if n != n.strip() or "  " in n or "\t" in n:
        weird_names.append((c["slug"], repr(n)))
print(f"\n[10] weirdly-formatted names: {len(weird_names)}")
for slug, n in weird_names[:5]:
    print(f"      {slug}: {n}")
