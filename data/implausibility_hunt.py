"""Implausibility hunter — built to catch the 'way too high' CLASS of bug (the
weighted-GPA over-credit that slipped through). Heavily exercises the recently
changed paths (weighted GPA single + year-by-year + not-offered, self_rigor,
legacy, EC extremes, the target haircut band) across many input combos, then
DECOMPOSES fit components and flags any cap exceeded or implausible odds."""
import random
import app
from candor_data import COLLEGES

random.seed(7)
def M(slug):
    c = next((x for x in COLLEGES if x["slug"] == slug or slug in x["slug"]), None)
    return app.merged_school(c) if c else None

# Broad basket — emphasize WEIGHTED-RANGE publics (where the bug lived) + a spread.
BASKET = [(c["name"], M(c["slug"])) for c in COLLEGES
          if c["slug"] in {"harvard","mit","ucla","ucb","umich","villanova","case",
                           "bu","northeastern","purdue","vt","clemson","uga","wisc",
                           "ucsd","gatech","alabama","arizona","fsu"}]
BASKET = [(n, s) for n, s in BASKET if s]

def gen(i):
    """Profiles that hammer the changed paths."""
    g = round(random.uniform(2.8, 4.0), 2)
    p = {"uw_gpa": g, "sat": random.choice([None, random.randint(1050,1580)]),
         "act": None, "major": random.choice(["Computer Science","Economics","Biology","Undecided","Business"]),
         "state": random.choice(["California","Texas","Ohio","Alabama","Georgia","Wisconsin",""]),
         "ecs": "x", "ec_rating": round(random.triangular(50,1000,400)),
         "spike_score": round(random.triangular(100,950,450)),
         "first_gen": random.random()<0.1, "athlete": random.random()<0.04}
    if p["sat"] is None and random.random()<0.5: p["act"]=random.randint(22,35)
    # WEIGHTED GPA paths — the risky one. Mix single field, year-by-year, scales.
    r = random.random()
    if r < 0.35:   # single weighted, varied scale incl inflated
        p["weighted_gpa"] = round(min(5.0, g + random.uniform(0.0, 1.2)), 2)
    elif r < 0.6:  # year-by-year, some not-offered, inflated junior/senior
        p["w_gpa_junior"] = round(min(5.0, g + random.uniform(0.2, 1.4)), 2)
        if random.random()<0.6: p["w_notoffered_freshman"]=1
        if random.random()<0.6: p["w_notoffered_sophomore"]=1
        if random.random()<0.5: p["w_gpa_senior"]=round(min(5.0,g+random.uniform(0.0,1.3)),2)
    # self_rigor path
    if random.random()<0.25: p["no_aps_offered"]=1; p["self_rigor"]=random.randint(3,10)
    # legacy
    if random.random()<0.1: p["legacy_schools"]=random.choice(["Michigan","UCLA","Villanova","Cornell University"])+f" {random.randint(1,4)}x"
    return p

profiles = [gen(i) for i in range(400)]

flags = []
stats = {"weighted_used":0, "gpa_comp_max":-99, "fit_max":0}
for i, p in enumerate(profiles):
    for name, sch in BASKET:
        try:
            fit, comp = app.compute_fit(p, sch)
            lo, hi = app.estimate_odds(sch, fit, p)
            mid = (lo+hi)/2
        except Exception as e:
            flags.append(("CRASH", i, name, f"{type(e).__name__}: {e}")); continue
        a = sch["accept"]
        gpa_c = comp.get("gpa", 0)
        weighted_range = (sch.get("gpa_hi") or 0) > 4.0
        hooked = p.get("athlete") or p.get("legacy_schools") or p.get("is_exceptional")
        stats["fit_max"] = max(stats["fit_max"], fit)
        if weighted_range and (p.get("weighted_gpa") or p.get("w_gpa_junior")):
            stats["weighted_used"] += 1
            stats["gpa_comp_max"] = max(stats["gpa_comp_max"], gpa_c)
            # CAP CHECK: weighted-GPA positive contribution must be <= +5 (the cap).
            if gpa_c > 5.01:
                flags.append(("GPA_CAP_EXCEEDED", i, name, f"gpa_comp={gpa_c} (cap is +5), w={p.get('weighted_gpa') or p.get('w_gpa_junior')}"))
        # OVER-CREDIT: a clearly sub-median-academic profile getting target/safety odds
        # at a selective school. This is the 'way too high' signature.
        weak_academ = (p.get("uw_gpa") or 0) < 3.7 and not p.get("weighted_gpa") and not p.get("w_gpa_junior")
        if not hooked and a < 0.30 and mid > a*100*2.5 + 5:
            flags.append(("ODDS_>2.5x_ACCEPT", i, name, f"{lo}-{hi}% vs {a*100:.0f}% accept (fit {fit}, gpa3.65? {p.get('uw_gpa')})"))
        # absurd ranges
        if hi > 95 or lo < 1 or lo > hi:
            flags.append(("RANGE", i, name, f"{lo}-{hi}%"))

print("="*78)
print(f"IMPLAUSIBILITY HUNT — {len(profiles)} profiles × {len(BASKET)} schools "
      f"= {len(profiles)*len(BASKET)} pairs")
print(f"weighted-GPA paths exercised: {stats['weighted_used']}  | max weighted GPA component seen: {stats['gpa_comp_max']:+.1f} (cap +5)")
print(f"max fit seen: {stats['fit_max']:.1f}")
print("="*78)

# group flags by type
from collections import Counter
by_type = Counter(f[0] for f in flags)
print("\nFLAGS BY TYPE:")
for t, n in by_type.items():
    print(f"  {t}: {n}")
if not flags:
    print("  (none)")

# show the worst over-credit cases (highest odds vs accept ratio)
oc = [f for f in flags if f[0]=="ODDS_>2.5x_ACCEPT"]
if oc:
    print(f"\nTOP 'WAY TOO HIGH' SUSPECTS ({len(oc)} total):")
    for typ,i,name,msg in oc[:12]:
        p=profiles[i]
        print(f"  P{i} {name}: {msg}")
        print(f"     gpa={p.get('uw_gpa')} w_gpa={p.get('weighted_gpa')} w_jr={p.get('w_gpa_junior')} sat={p.get('sat')} act={p.get('act')} ec={p.get('ec_rating')} spike={p.get('spike_score')}")

# critical cap/crash failures
crit = [f for f in flags if f[0] in ("GPA_CAP_EXCEEDED","CRASH","RANGE")]
print(f"\nCRITICAL (cap exceeded / crash / bad range): {len(crit)}")
for f in crit[:10]: print("  ", f)

print("\nVERDICT:", "CLEAN — no cap breaches, no crashes, no bad ranges" if not crit else "NEEDS REVIEW")
print("DONE")
