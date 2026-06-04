"""Calibration before/after harness.
Runs reference applicant profiles across an anchor set of schools and prints
fit + odds next to each school's real accept rate, so we can see where odds
are inflated and whether a change cascades. Read-only; imports app.py.

Usage:  python3 scripts/calib_harness.py [label]
"""
import sys, json
sys.path.insert(0, ".")
import app

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"

def prof(**k):
    base = dict(uw_gpa=None, weighted_gpa=None, sat=None, act=None,
                major="Economics", state="Texas", school_type="private",
                ecs="", leadership="", awards="", legacy_schools="")
    base.update(k)
    return base

# Unhooked reference applicants (no legacy/athlete), out-of-state.
PROFILES = {
    "STRONG": prof(uw_gpa=3.97, sat=1540,
                   ecs="Founded research nonprofit, 2 papers, state science fair winner",
                   leadership="President of 2 clubs, varsity captain"),
    "MID":    prof(uw_gpa=3.75, sat=1380,
                   ecs="Club member, part-time job, volunteering",
                   leadership="Club officer"),
    "WEAK":   prof(uw_gpa=3.40, sat=1200, ecs="Some clubs", leadership=""),
}

# Jasper's real (test-less) profile, incl. Cornell legacy x4 — to watch hook-stacking.
JASPER = dict(uw_gpa=3.65, weighted_gpa=3.85, sat=None, act=None,
              gpa_freshman=3.24, gpa_sophomore=3.8, gpa_junior=3.96,
              major="Information Systems Management", state="California", school_type="private",
              ecs="Aquarium volunteer 100+hrs; podcast co-host; pizza job; neurosurgeon shadow; scuba",
              leadership="Varsity VB captain 4yr; club VB captain; founder marine bio club",
              legacy_schools="Cornell 4x", awards="")
JASPER_WITH_ACT = dict(JASPER, act=33)

SCHOOLS = ["harvard","stanford","mit","princeton","cornell","upenn","duke","uchicago",
           "usc","nyu","northeastern","bu","tufts","villanova","bucknell",
           "ucla","umich","wisconsin","tamu","penn-state"]

def odds_for(p, m):
    f, _ = app.compute_fit(p, m)
    lo, hi = app.estimate_odds(m, f, p)
    return f, lo, hi

rows = []
for s in SCHOOLS:
    sd = app.COLLEGES_BY_SLUG.get(s)
    if not sd:
        continue
    m = app.merged_school(sd)
    acc = round(m["accept"] * 100, 1)
    rec = {"school": s, "acc": acc}
    for name, p in PROFILES.items():
        f, lo, hi = odds_for(p, m)
        rec[name] = f"{lo}-{hi}%"
        rec[name + "_mid"] = (lo + hi) / 2
    # Jasper, with and without test, to track the test-sensitivity bug
    fj, lj, hj = odds_for(JASPER, m)
    fja, lja, hja = odds_for(JASPER_WITH_ACT, m)
    rec["JASPER_notest"] = f"{lj}-{hj}%"
    rec["JASPER_act33"] = f"{lja}-{hja}%"
    rec["JASPER_swing"] = (lja + hja) / 2 - (lj + hj) / 2
    rows.append(rec)

# Print human table
print(f"\n=== {LABEL} ===")
hdr = f'{"school":12}{"acc%":>6} | {"STRONG":>9}{"MID":>9}{"WEAK":>9} | {"J.notest":>9}{"J.ACT33":>9}{"swing":>6}'
print(hdr); print("-" * len(hdr))
for r in rows:
    print(f'{r["school"]:12}{r["acc"]:>6} | {r["STRONG"]:>9}{r["MID"]:>9}{r["WEAK"]:>9} | '
          f'{r["JASPER_notest"]:>9}{r["JASPER_act33"]:>9}{r["JASPER_swing"]:>+6.0f}')

# Dump machine-readable for diffing
with open(f"/tmp/calib_{LABEL}.json", "w") as fh:
    json.dump(rows, fh, indent=2)

# Anchor sanity gate: no sub-7%-accept school should show an UNHOOKED STRONG
# applicant above 15% (the 5/31 over-inflation signature).
print("\n--- anchor gate (sub-7% schools, STRONG unhooked) ---")
breaches = []
for r in rows:
    if r["acc"] < 7.0 and r["STRONG_mid"] > 15:
        breaches.append(f'  BREACH {r["school"]} acc {r["acc"]}% -> STRONG {r["STRONG"]}')
print("\n".join(breaches) if breaches else "  OK — no elite inflation")
