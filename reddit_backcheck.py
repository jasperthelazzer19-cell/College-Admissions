"""Back-check Candor's odds engine against real r/collegeresults outcomes.
For every (profile -> school -> actual outcome), run Candor's REAL deterministic
odds engine (compute_fit + estimate_odds, no LLM/API) and compare the predicted
admit odds to what actually happened. Pure local compute = $0.

Outputs a calibration report: for each predicted-odds bucket, the real admit rate.
Well-calibrated => real admit rate ~= predicted midpoint.
"""
import os, sys, json, re, warnings
warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w")  # silence import-time noise
import app
sys.stderr = sys.__stderr__

COLLEGES = app.COLLEGES

# ---- school name -> Candor slug matcher ----
def norm(s):
    # NOTE: do NOT strip "state" — that collapses UC vs Cal State campuses
    # ("University of California, Los Angeles" vs "California State, LA").
    s = (s or "").lower().replace("&", "and").replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(university|college|the|of|at)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

SLUG = {c["slug"]: c for c in COLLEGES}
by_norm, by_name = {}, {}
for c in COLLEGES:
    by_name[c["name"].lower()] = c
    k = norm(c["name"])
    if k and k not in by_norm:
        by_norm[k] = c

ALIAS = {
    "massachusetts institute of technology": "mit", "georgia institute of technology": "gatech",
    "university of california los angeles": "ucla", "university of california berkeley": "ucb",
    "university of california san diego": "ucsd", "university of california irvine": "uci",
    "university of california davis": "ucdavis", "university of california santa barbara": "ucsb",
    "university of southern california": "usc", "university of michigan": "umich",
    "university of north carolina": "unc", "university of north carolina chapel hill": "unc",
    "university of virginia": "uva", "university of texas austin": "ut-austin",
    "university of illinois urbana champaign": "uiuc", "new york university": "nyu",
    "carnegie mellon university": "cmu", "university of pennsylvania": "upenn",
    "university of florida": "uf", "university of washington": "uw", "university of wisconsin": "wisc",
    "university of wisconsin madison": "wisc", "university of maryland": "umd",
    "university of maryland college park": "umd", "ohio state university": "osu",
    "pennsylvania state university": "penn-state", "michigan state university": "msu",
    "purdue university": "purdue", "boston university": "bu", "boston college": "bc",
    "johns hopkins university": "jhu", "university of chicago": "uchicago", "uchicago": "uchicago",
    "northwestern university": "northwestern", "cornell university": "cornell",
}

ALIAS_N = {norm(k): v for k, v in ALIAS.items()}  # normalize alias keys

def match(school_name):
    nm = (school_name or "").lower().strip()
    if nm in by_name: return by_name[nm]
    k = norm(school_name)
    if k in ALIAS_N and ALIAS_N[k] in SLUG: return SLUG[ALIAS_N[k]]
    if k in by_norm: return by_norm[k]
    return None

# ---- build a Candor profile dict from a reddit profile ----
def to_candor_profile(d):
    return {
        "uw_gpa": d.get("gpa_uw"), "weighted_gpa": d.get("gpa_w"),
        "sat": d.get("sat"), "act": d.get("act"),
        "state": d.get("state") if d.get("state") not in ("International", "") else "",
        "major": d.get("major") or "",
        "first_gen": bool(d.get("first_gen")), "athlete": "recruited_athlete" in (d.get("hooks") or []),
        "is_international": bool(d.get("international")),
        "ecs": d.get("ecs") or "", "awards": d.get("awards") or "", "leadership": "",
    }

def predict(profile, school):
    merged = app.merged_school(school)
    fit, _ = app.compute_fit(profile, merged)
    low, high = app.estimate_odds(merged, fit, profile)
    return (low + high) / 2.0  # predicted admit % midpoint

def main():
    profs = json.load(open("reddit_calibration.json"))
    samples = []   # (pred_mid, admitted(0/1), school_slug, tier, accept_rate, round)
    matched, unmatched = 0, {}
    for d in profs:
        cp = to_candor_profile(d)
        # need at least GPA or a test score to predict meaningfully
        if cp["uw_gpa"] is None and cp["sat"] is None and cp["act"] is None:
            continue
        for r in (d.get("results") or []):
            outcome = r.get("outcome")
            if outcome == "deferred":
                continue  # no final decision
            admitted = 1 if outcome == "accepted" else 0  # rejected/waitlisted = not admitted
            sch = match(r.get("school", ""))
            if not sch:
                unmatched[r.get("school", "")] = unmatched.get(r.get("school", ""), 0) + 1
                continue
            try:
                pm = predict(cp, sch)
            except Exception:
                continue
            matched += 1
            samples.append((pm, admitted, sch["slug"], sch.get("tier", 5),
                            sch.get("accept", 0), r.get("round", "")))

    print(f"matched {matched} outcomes to Candor schools "
          f"({len(unmatched)} unmatched school names)\n")

    # ---- calibration buckets ----
    buckets = [(0,5),(5,10),(10,20),(20,35),(35,50),(50,70),(70,101)]
    print(f"{'predicted odds':>15} | {'n':>5} | {'mean pred':>9} | {'ACTUAL admit':>12} | gap")
    print("-"*62)
    for lo, hi in buckets:
        grp = [s for s in samples if lo <= s[0] < hi]
        if not grp: continue
        n = len(grp); mp = sum(s[0] for s in grp)/n; actual = 100*sum(s[1] for s in grp)/n
        gap = actual - mp
        flag = "  <-- OVER-rating" if gap < -6 else ("  <-- under-rating" if gap > 6 else "")
        print(f"{f'{lo}-{hi if hi<101 else 100}%':>15} | {n:>5} | {mp:>8.1f}% | {actual:>11.1f}% | {gap:+5.1f}{flag}")

    # ---- per-tier ----
    print(f"\n{'tier':>6} | {'n':>5} | {'mean pred':>9} | {'ACTUAL admit':>12} | gap")
    print("-"*52)
    for t in (1,2,3,4,5):
        grp = [s for s in samples if s[3] == t]
        if not grp: continue
        n=len(grp); mp=sum(s[0] for s in grp)/n; actual=100*sum(s[1] for s in grp)/n
        print(f"{t:>6} | {n:>5} | {mp:>8.1f}% | {actual:>11.1f}% | {actual-mp:+5.1f}")

    # ---- worst per-school miscalibrations (>=15 samples) ----
    from collections import defaultdict
    bysch = defaultdict(list)
    for s in samples: bysch[s[2]].append(s)
    rows = []
    for slug, grp in bysch.items():
        if len(grp) < 15: continue
        mp=sum(s[0] for s in grp)/len(grp); actual=100*sum(s[1] for s in grp)/len(grp)
        rows.append((actual-mp, slug, len(grp), mp, actual))
    rows.sort(key=lambda x: x[0])
    print(f"\nmost MIScalibrated schools (n>=15):   gap = actual - predicted")
    print(f"{'school':>16} | {'n':>4} | {'pred':>6} | {'actual':>7} | gap")
    print("-"*52)
    for gap, slug, n, mp, actual in rows[:8] + rows[-8:]:
        print(f"{slug:>16} | {n:>4} | {mp:>5.1f}% | {actual:>6.1f}% | {gap:+5.1f}")

    # overall
    n=len(samples); mp=sum(s[0] for s in samples)/n; actual=100*sum(s[1] for s in samples)/n
    print(f"\nOVERALL: n={n}  mean predicted {mp:.1f}%  actual admit {actual:.1f}%  gap {actual-mp:+.1f}")
    # top unmatched (so we can add aliases later)
    top_un = sorted(unmatched.items(), key=lambda x:-x[1])[:12]
    print("\ntop unmatched school names:", ", ".join(f"{k}({v})" for k,v in top_un))

if __name__ == "__main__":
    main()
