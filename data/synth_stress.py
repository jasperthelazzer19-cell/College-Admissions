"""Synthetic stress-test: generate 100 diverse profiles spanning the full input
space, run them through the real odds engine at a basket of schools across the
selectivity spectrum, and check INTERNAL SANITY (monotonicity, tier behavior,
no absurd outputs, edge-case robustness). No real outcomes -> not an accuracy
test; this catches calibration/logic bugs, not miscalibration vs truth."""
import random, statistics
import app
from candor_data import COLLEGES

random.seed(42)  # deterministic
M = lambda slug: app.merged_school(next(c for c in COLLEGES if c["slug"] == slug))

# Basket across selectivity (accept rate ascending)
BASKET = []
for slug in ["harvard","mit","ucla","northeastern","bu","umich","villanova",
             "case","purdue"]:
    c = next((x for x in COLLEGES if x["slug"] == slug or slug in x["slug"]), None)
    if c: BASKET.append((c["name"], app.merged_school(c)))

MAJORS = ["Computer Science","Economics","Biology","English","Mechanical Engineering",
          "Political Science","Business","Mathematics","Information Systems","Undecided"]
STATES = ["California","Texas","New York","Massachusetts","Ohio","Florida","Michigan",""]

def rand_profile(i):
    """Generate one profile. Spread across weak->elite, with edge cases sprinkled."""
    tier = random.random()
    # academic strength tiers
    if tier < 0.15:      gpa, sat = random.uniform(2.7,3.3), random.randint(1000,1250)   # weak
    elif tier < 0.45:    gpa, sat = random.uniform(3.3,3.7), random.randint(1250,1400)   # average
    elif tier < 0.75:    gpa, sat = random.uniform(3.7,3.9), random.randint(1380,1500)   # solid
    elif tier < 0.93:    gpa, sat = random.uniform(3.85,3.97), random.randint(1480,1560) # strong
    else:                gpa, sat = random.uniform(3.93,4.0), random.randint(1540,1600)  # elite
    p = {
        "uw_gpa": round(gpa,2),
        "sat": sat if random.random() > 0.2 else None,        # 20% test-optional
        "act": None,
        "major": random.choice(MAJORS),
        "state": random.choice(STATES),
        "ecs": "x",
        "ec_rating": round(random.triangular(50, 1000, 350)),  # most mid, some high
        "spike_score": round(random.triangular(100, 950, 400)),
        "first_gen": random.random() < 0.12,
        "athlete": random.random() < 0.05,
        "is_international": random.random() < 0.10,
    }
    if p["sat"] is None and random.random() < 0.5:
        p["act"] = random.randint(24, 35)
    # ~30% have a weighted GPA + sometimes year-by-year
    if random.random() < 0.30:
        p["weighted_gpa"] = round(min(4.5, gpa + random.uniform(0.1, 0.6)), 2)
    # ~20% rigor self-rating (no-AP schools)
    if random.random() < 0.20:
        p["no_aps_offered"] = 1
        p["self_rigor"] = random.randint(4, 10)
    # ~8% legacy at a basket school
    if random.random() < 0.08:
        p["legacy_schools"] = random.choice(["Harvard","Michigan","Cornell University"]) + " 1x"
    # Edge cases for specific indices
    if i == 0:   p = {"uw_gpa": None, "sat": None, "major":"", "ecs":"", "ec_rating":1, "spike_score":1}  # empty
    if i == 1:   p.update({"uw_gpa":4.0,"sat":1600,"ec_rating":1000,"spike_score":950,"athlete":True})     # max
    if i == 2:   p.update({"uw_gpa":2.0,"sat":900,"ec_rating":1,"spike_score":1})                          # floor
    return p

def strength_index(p):
    """A rough composite 'how strong' for monotonicity checks."""
    g = (p.get("uw_gpa") or 0) / 4.0
    s = ((p.get("sat") or (app._normalize_score(None,p.get("act")) if p.get("act") else 0)) or 0) / 1600.0
    e = (p.get("ec_rating") or 0) / 1000.0
    return g*0.45 + s*0.30 + e*0.25

# ── Generate + score ──────────────────────────────────────
profiles = [rand_profile(i) for i in range(100)]
rows = []  # (pidx, strength, school_name, accept, fit, lo, hi, mid)
errors = []
for i, p in enumerate(profiles):
    try:
        si = strength_index(p)
    except Exception as e:
        si = 0
    for name, sch in BASKET:
        try:
            fit, _ = app.compute_fit(p, sch)
            lo, hi = app.estimate_odds(sch, fit, p)
            rows.append((i, si, name, sch["accept"], fit, lo, hi, (lo+hi)/2))
        except Exception as e:
            errors.append(f"profile {i} @ {name}: {type(e).__name__}: {e}")

print("="*78)
print(f"SYNTHETIC STRESS TEST — {len(profiles)} profiles × {len(BASKET)} schools = {len(rows)} odds")
print(f"Errors/crashes: {len(errors)}")
for e in errors[:5]: print("  ", e)
print("="*78)

# ── 1. Per-school odds distribution ──
print("\n1. ODDS DISTRIBUTION PER SCHOOL (midpoint %)")
print(f"   {'School':<16}{'accept':>7}  {'min':>4} {'p25':>4} {'med':>4} {'p75':>4} {'max':>4}  sane?")
for name, sch in BASKET:
    mids = sorted(r[7] for r in rows if r[2] == name)
    if not mids: continue
    q = lambda f: mids[min(len(mids)-1, int(len(mids)*f))]
    a = sch["accept"]*100
    # sanity: at a <6% school, even the max shouldn't blow past ~25% w/o a hook;
    # at a >40% school, the strongest quartile should clear the accept rate.
    flag = "ok"
    if a < 6 and q(0.99) > 35: flag = "HIGH max"
    if a > 45 and q(0.9) < a*0.8: flag = "LOW top"
    print(f"   {name:<16}{a:>6.1f}%  {mids[0]:>4.0f} {q(.25):>4.0f} {q(.5):>4.0f} {q(.75):>4.0f} {mids[-1]:>4.0f}  {flag}")

# ── 2. Monotonicity: Spearman(strength, odds) per school ──
print("\n2. MONOTONICITY — Spearman corr between profile strength and odds (want ~+1)")
def spearman(xs, ys):
    n=len(xs)
    rx={v:i for i,v in enumerate(sorted(range(n), key=lambda k:xs[k]))}
    ry={v:i for i,v in enumerate(sorted(range(n), key=lambda k:ys[k]))}
    dx=[rx[i] for i in range(n)]; dy=[ry[i] for i in range(n)]
    mx=sum(dx)/n; my=sum(dy)/n
    cov=sum((dx[i]-mx)*(dy[i]-my) for i in range(n))
    sx=(sum((d-mx)**2 for d in dx))**.5; sy=(sum((d-my)**2 for d in dy))**.5
    return cov/(sx*sy) if sx*sy else 0
for name, sch in BASKET:
    pairs=[(r[1], r[7]) for r in rows if r[2]==name]
    if len(pairs)<10: continue
    rho=spearman([a for a,_ in pairs],[b for _,b in pairs])
    bar = "█"*int(rho*20)
    print(f"   {name:<16} rho={rho:+.3f}  {bar}")

# ── 3. Within-profile tier ordering: does each profile's odds rank by accept rate? ──
print("\n3. TIER ORDERING — for each profile, are odds monotincreasing with accept rate?")
inv = 0
for i in range(len(profiles)):
    pr = sorted([(r[3], r[7]) for r in rows if r[0]==i])  # by accept rate
    odds_seq = [o for _,o in pr]
    # count inversions (a more-selective school giving higher odds than a less-selective one)
    bad = sum(1 for a in range(len(odds_seq)) for b in range(a+1,len(odds_seq)) if odds_seq[a] > odds_seq[b]+8)
    if bad: inv += 1
print(f"   profiles with a >8pt tier inversion (selective school beating a safety): {inv}/{len(profiles)}")

# ── 4. Sample: weakest, median, strongest profiles ──
print("\n4. SAMPLE PROFILES (sorted by strength)")
order = sorted(range(len(profiles)), key=lambda i: strength_index(profiles[i]))
for label, i in [("WEAKEST", order[3]), ("25th", order[25]), ("MEDIAN", order[50]),
                 ("75th", order[75]), ("STRONGEST", order[-2])]:
    p=profiles[i]
    od = {r[2].split()[0]: f"{r[5]}-{r[6]}" for r in rows if r[0]==i}
    print(f"   [{label}] gpa={p.get('uw_gpa')} sat={p.get('sat')} act={p.get('act')} ec={p.get('ec_rating')} "
          f"spike={p.get('spike_score')} hooks={'L' if p.get('legacy_schools') else ''}{'A' if p.get('athlete') else ''}{'F' if p.get('first_gen') else ''}")
    print(f"             Harvard {od.get('Harvard','?')} | Northeastern {od.get('Northeastern','?')} | "
          f"Villanova {od.get('Villanova','?')} | Purdue {od.get('Purdue','?')}")

# ── 5. Absurdity scan ──
print("\n5. ABSURDITY SCAN")
absurd=[]
for r in rows:
    pidx, si, name, acc, fit, lo, hi, mid = r
    if acc < 0.05 and mid > 40: absurd.append(f"{name} ({acc*100:.0f}%) gave {lo}-{hi}% (profile {pidx})")
    if hi > 95 or lo < 1: absurd.append(f"{name} out-of-range {lo}-{hi}% (profile {pidx})")
    if lo > hi: absurd.append(f"{name} lo>hi {lo}-{hi}% (profile {pidx})")
print(f"   absurd outputs: {len(absurd)}")
for a in absurd[:8]: print("   ", a)
print("\nDONE")
