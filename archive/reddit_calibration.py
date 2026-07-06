"""Extract structured (profile -> school -> outcome) calibration rows from
harvested r/collegeresults posts. Reads reddit_results_raw.json (pulled via
the browser, no API key), uses Haiku to parse each post into strict JSON.

Run so the key stays in env, never printed:
    railway run python3 reddit_calibration.py --limit 12      # sample
    railway run python3 reddit_calibration.py                 # full

Outputs:
    reddit_calibration.json       full profiles w/ results list
    reddit_calibration_rows.csv   one row per (profile, school, outcome)
"""
import os, sys, json, csv, argparse, re
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic

KEY = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
if not KEY:
    sys.exit("No ANTHROPIC_KEY in env. Run via `railway run python3 reddit_calibration.py` "
             "or export ANTHROPIC_KEY first.")
client = anthropic.Anthropic(api_key=KEY)
MODEL = "claude-haiku-4-5-20251001"

SYSTEM = """You extract structured admissions data from a Reddit r/collegeresults post.
Output ONLY one JSON object, no prose, no code fences. Schema (use null/"" when unknown,
NEVER invent numbers not in the text):

{
 "gpa_uw": float (unweighted /4.0) or null,
 "gpa_w": float (weighted) or null,
 "sat": int (total 400-1600) or null,
 "act": int (composite 1-36) or null,
 "rank_pct": int (class rank as top X percent, e.g. 5) or null,
 "state": "full US state name or '' (or 'International')",
 "gender": "M/F/'' ",
 "race": "short race/ethnicity if stated, else ''",
 "first_gen": bool,
 "international": bool,
 "income": "low | middle | high | '' (only if clearly stated/implied)",
 "major": "intended major or ''",
 "hooks": ["recruited_athlete","legacy","first_gen","low_income","URM","questbridge", ... only those clearly present],
 "ecs": "one-line summary of the strongest extracurriculars",
 "awards": "one-line summary of awards/honors",
 "results": [
   {"school": "clean school name (e.g. 'University of Michigan')",
    "outcome": "accepted | rejected | waitlisted | deferred",
    "round": "ED|EA|REA|ED2|EA2|RD|rolling|''"}
 ]
}

Map every admit/deny/WL/defer the post lists into results. Normalize outcome:
admitted/accepted/committed -> accepted; denied/rejected -> rejected; WL -> waitlisted;
deferred->rejected ONLY if later denied, otherwise 'deferred'. If a school appears with a
final accept after a defer, record accepted. Skip schools with no clear decision."""


def extract(post):
    body = post["title"] + "\n\n" + post["self"]
    body = body[:9000]
    try:
        m = client.messages.create(model=MODEL, max_tokens=1500, temperature=0,
                                   system=SYSTEM, messages=[{"role": "user", "content": body}])
        raw = m.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`"); raw = raw[raw.find("{"):]
        i, j = raw.find("{"), raw.rfind("}")
        d = json.JSONDecoder(strict=False).decode(raw[i:j+1])
        d["_id"] = post["id"]; d["_url"] = post["url"]; d["_score"] = post["score"]
        return d
    except Exception as e:
        return {"_id": post["id"], "_url": post["url"], "_error": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    posts = json.load(open("reddit_results_raw.json"))
    if args.offset: posts = posts[args.offset:]
    if args.limit: posts = posts[:args.limit]
    print(f"extracting {len(posts)} posts with {MODEL} ...")

    out = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(extract, p): p for p in posts}
        for f in as_completed(futs):
            out.append(f.result()); done += 1
            if done % 25 == 0: print(f"  {done}/{len(posts)}")

    json.dump(out, open("reddit_calibration.json", "w"), indent=1)
    # flatten to rows
    rows = []
    ok = 0
    for d in out:
        if d.get("_error") or not d.get("results"): continue
        ok += 1
        for r in d["results"]:
            rows.append({
                "school": r.get("school", ""), "outcome": r.get("outcome", ""),
                "round": r.get("round", ""), "gpa_uw": d.get("gpa_uw"), "gpa_w": d.get("gpa_w"),
                "sat": d.get("sat"), "act": d.get("act"), "rank_pct": d.get("rank_pct"),
                "state": d.get("state", ""), "first_gen": d.get("first_gen"),
                "international": d.get("international"), "income": d.get("income", ""),
                "major": d.get("major", ""), "hooks": "|".join(d.get("hooks") or []),
                "url": d.get("_url", ""),
            })
    with open("reddit_calibration_rows.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["school"])
        w.writeheader(); w.writerows(rows)
    errs = sum(1 for d in out if d.get("_error"))
    print(f"\nDONE. {ok} profiles parsed, {len(rows)} result-rows, {errs} errors.")
    print(f"-> reddit_calibration.json / reddit_calibration_rows.csv")
    # quick peek
    for d in out[:3]:
        if d.get("_error"): continue
        print(f"  GPA {d.get('gpa_uw')} SAT {d.get('sat')} {d.get('state')} -> "
              + ", ".join(f"{r['school']}:{r['outcome']}" for r in (d.get('results') or [])[:5]))


if __name__ == "__main__":
    main()
