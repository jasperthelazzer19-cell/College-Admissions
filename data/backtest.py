import os, re, json, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import app
from candor_data import COLLEGES

app._claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])
NM = app._name_to_slug_map()

ALIASES = {
    "university of pennsylvania": "upenn", "penn": "upenn", "u penn": "upenn",
    "georgia institute of technology": "gatech", "georgia tech": "gatech",
    "uc berkeley": "ucb", "berkeley": "ucb", "cal": "ucb",
    "university of california berkeley": "ucb", "university of california, berkeley": "ucb",
    "university of california los angeles": "ucla", "university of california, los angeles": "ucla",
    "university of michigan": "umich", "umich": "umich", "michigan": "umich",
    "university of southern california": "usc", "usc": "usc",
    "carnegie mellon": "cmu", "carnegie mellon university": "cmu", "cmu": "cmu",
    "ut austin": "utaustin", "university of texas at austin": "utaustin", "utexas": "utaustin",
    "unc": "unc", "unc chapel hill": "unc", "university of north carolina": "unc",
    "uiuc": "uiuc", "university of illinois": "uiuc", "illinois urbana champaign": "uiuc",
    "uw madison": "wisconsin", "university of wisconsin": "wisconsin", "wisconsin madison": "wisconsin",
    "uva": "uva", "university of virginia": "uva",
    "ucsd": "ucsd", "uc san diego": "ucsd", "uc davis": "ucdavis", "ucsb": "ucsb", "uc santa barbara": "ucsb",
    "uc irvine": "uci", "uci": "uci",
    "johns hopkins": "jhu", "jhu": "jhu", "hopkins": "jhu",
    "notre dame": "notredame", "boston college": "bc", "boston university": "bu",
    "new york university": "nyu", "nyu": "nyu", "northeastern university": "northeastern",
    "washington university in st louis": "wustl", "wash u": "wustl", "washu": "wustl", "wustl": "wustl",
    "georgia": "uga", "university of georgia": "uga", "uf": "florida", "university of florida": "florida",
    "umd": "umd", "university of maryland": "umd", "purdue university": "purdue",
    "rice university": "rice", "vanderbilt university": "vanderbilt", "emory university": "emory",
    "tufts university": "tufts", "william and mary": "williammary", "william & mary": "williammary",
}

def slugify(name):
    n = (name or "").strip().lower()
    n = re.sub(r"\(.*?\)", "", n).strip()
    n = n.replace("the ", "")
    if n in NM: return NM[n]
    if n in ALIASES: return ALIASES[n]
    n2 = n.replace("university of ", "").replace(" university", "").strip()
    if n2 in NM: return NM[n2]
    if n2 in ALIASES: return ALIASES[n2]
    return None

# ---- split profiles ----
text = open(os.path.join(os.path.dirname(__file__), "manual_profiles_49.md"), encoding="utf-8").read()
blocks = re.split(r"## PROFILE \d+", text)[1:]
print(f"profile blocks: {len(blocks)}", file=sys.stderr)

PARSE_SYS = "You extract structured data from messy Reddit college-results posts. Output ONLY valid JSON."
def parse_profile(i, block):
    block = block[:4500]
    user = (
        "From this r/collegeresults post, extract JSON:\n"
        '{"gpa_uw": <unweighted GPA on 4.0 scale, float, best estimate>, '
        '"sat": <int or null>, "act": <int or null>, "major": "<intended major>", '
        '"ecs": "<all extracurriculars as one string>", "awards": "<all awards/honors>", '
        '"leadership": "<leadership roles>", '
        '"first_gen": <bool>, "legacy": "<school name or empty>", "athlete": <recruited athlete bool>, '
        '"international": <bool>, "state": "<US state of residence, full name, or empty>", '
        '"results": [{"school": "<name>", "outcome": "accepted|rejected|waitlisted|deferred"}]}\n'
        "Include EVERY school in their results with the correct outcome. If GPA is on another scale, convert to 4.0.\n\n"
        f"POST:\n{block}"
    )
    raw = app._claude("claude-haiku-4-5-20251001", PARSE_SYS, user, max_tokens=1500)
    if not raw: return i, None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m: return i, None
    try:
        return i, json.loads(m.group(0))
    except Exception:
        return i, None

parsed = {}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(parse_profile, i, b) for i, b in enumerate(blocks)]
    for f in as_completed(futs):
        i, d = f.result()
        if d: parsed[i] = d
print(f"parsed ok: {len(parsed)}/{len(blocks)}", file=sys.stderr)

# ---- grade + odds ----
COL = {c["slug"]: c for c in COLLEGES}
rows = []  # (pid, school, slug, outcome, lo, hi, mid, fit, ec, spike, accept)
def process(i, d):
    prof = {
        "uw_gpa": d.get("gpa_uw"), "sat": d.get("sat"), "act": d.get("act"),
        "major": d.get("major") or "", "ecs": d.get("ecs") or "",
        "awards": d.get("awards") or "", "leadership": d.get("leadership") or "",
        "first_gen": bool(d.get("first_gen")), "athlete": bool(d.get("athlete")),
        "is_international": bool(d.get("international")),
        "legacy_schools": d.get("legacy") or "",
        "state": d.get("state") or "",
    }
    ec, sp = app.grade_ec_and_spike(prof)
    if ec is None: ec = app.ec_rating_keyword(prof)
    if sp is None: sp = app.cohesion_keyword(prof)
    prof["ec_rating"], prof["spike_score"] = ec, sp
    out = []
    for r in (d.get("results") or []):
        slug = slugify(r.get("school"))
        if not slug or slug not in COL: continue
        outcome = (r.get("outcome") or "").lower()
        if outcome not in ("accepted", "rejected", "waitlisted", "deferred"): continue
        school = app.merged_school(COL[slug])
        fit, _ = app.compute_fit(prof, school)
        lo, hi = app.estimate_odds(school, fit, prof)
        out.append((i, school["name"], slug, outcome, lo, hi, (lo+hi)/2, fit, ec, sp, school["accept"]))
    return out

with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(process, i, d) for i, d in parsed.items()]
    for f in as_completed(futs):
        rows.extend(f.result())

json.dump(rows, open(os.path.join(os.path.dirname(__file__), "backtest_rows.json"), "w"))
print(f"applicant-school pairs scored: {len(rows)}", file=sys.stderr)

# ---- analysis ----
def stats(items):
    if not items: return (0, 0, 0)
    mids = [r[6] for r in items]
    return (len(items), sum(mids)/len(mids), sorted(mids)[len(mids)//2])

acc = [r for r in rows if r[3] == "accepted"]
rej = [r for r in rows if r[3] == "rejected"]
wl  = [r for r in rows if r[3] in ("waitlisted", "deferred")]

print("\n================ BACKTEST: 49 r/collegeresults profiles ================")
print(f"Total applicant-school pairs matched to Candor: {len(rows)}")
print(f"  accepted={len(acc)}  rejected={len(rej)}  waitlist/deferred={len(wl)}")
print("\n--- Mean PREDICTED odds by ACTUAL outcome (discrimination) ---")
for label, items in [("ACCEPTED", acc), ("WAITLIST/DEFER", wl), ("REJECTED", rej)]:
    n, mean, med = stats(items)
    print(f"  {label:<15} n={n:<4} mean predicted odds = {mean:5.1f}%   median = {med:4.1f}%")

# elite subset (accept < 10%)
print("\n--- ELITE schools only (accept rate < 10%) ---")
for label, items in [("ACCEPTED", acc), ("REJECTED", rej)]:
    sub = [r for r in items if r[10] < 0.10]
    n, mean, med = stats(sub)
    print(f"  {label:<15} n={n:<4} mean predicted odds = {mean:5.1f}%   median = {med:4.1f}%")

# within-applicant discrimination: for applicants with >=1 accept and >=1 reject,
# does mean(accept odds) > mean(reject odds)?
from collections import defaultdict
byp = defaultdict(lambda: {"a": [], "r": []})
for r in rows:
    if r[3] == "accepted": byp[r[0]]["a"].append(r[6])
    elif r[3] == "rejected": byp[r[0]]["r"].append(r[6])
both = [(p, d) for p, d in byp.items() if d["a"] and d["r"]]
correct = sum(1 for p, d in both if sum(d["a"])/len(d["a"]) > sum(d["r"])/len(d["r"]))
print(f"\n--- Within-applicant ranking (applicants with >=1 accept AND >=1 reject) ---")
print(f"  applicants: {len(both)}  | model gave their ACCEPTS higher avg odds than REJECTS: {correct}/{len(both)}")

# calibration buckets
print("\n--- Calibration: actual admit rate within each predicted-odds bucket ---")
buckets = [(0,5),(5,10),(10,20),(20,40),(40,101)]
pairs = [(r[6], 1 if r[3]=="accepted" else 0) for r in rows if r[3] in ("accepted","rejected")]
for lo,hi in buckets:
    b=[o for m,o in pairs if lo<=m<hi]
    if b: print(f"  predicted {lo:>2}-{hi:<3}%: n={len(b):<4} actual admit rate = {100*sum(b)/len(b):4.1f}%  (survivorship-inflated)")

# biggest misses: rejected but high predicted, accepted but very low predicted
print("\n--- Notable: REJECTED with highest predicted odds ---")
for r in sorted(rej, key=lambda x:-x[6])[:6]:
    print(f"  P{r[0]+1:<2} {r[1]:<22} pred {r[4]}-{r[5]}%  (fit {r[7]}, ec {r[8]:.0f}, spike {r[9]:.0f})")
print("--- Notable: ACCEPTED with lowest predicted odds ---")
for r in sorted(acc, key=lambda x:x[6])[:6]:
    print(f"  P{r[0]+1:<2} {r[1]:<22} pred {r[4]}-{r[5]}%  (fit {r[7]}, ec {r[8]:.0f}, spike {r[9]:.0f})")
