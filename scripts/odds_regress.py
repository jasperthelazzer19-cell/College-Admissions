#!/usr/bin/env python3
"""Odds regression gate: the numbers must not move unless you moved them on purpose.

Computes fit + odds for a fixed grid of real stored profiles x schools on the
deterministic path (no LLM keys), plus the round-split table, and compares
against the committed baseline. Any drift fails the run.

  PYTHONPATH=. python3 scripts/odds_regress.py                  # compare (CI/pre-push)
  PYTHONPATH=. python3 scripts/odds_regress.py --write-baseline # intentional recalibration

Hermetic: runs against a throwaway copy of college.db so in-app writes
(compute_fit re-grades, owner grants, meter resets) never contaminate results.
"""
import os, sys, json, shutil, tempfile

os.environ.pop("ANTHROPIC_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mktemp(suffix=".db")
shutil.copy(os.path.join(REPO, "college.db"), _tmp)
os.environ["DB_PATH"] = _tmp
os.environ.setdefault("ODDS_ENGINE", "v2")
sys.path.insert(0, REPO)

import sqlite3
import app  # noqa: E402

BASELINE = os.path.join(REPO, "scripts", "odds_baseline.json")
SCHOOLS = ["stanford", "harvard", "tulane", "ucla", "university-of-michigan",
           "nyu", "penn-state", "arizona-state", "northeastern", "boston-university"]


def snapshot():
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT * FROM profiles WHERE uw_gpa IS NOT NULL
                           AND ec_rating IS NOT NULL ORDER BY user_id LIMIT 6""").fetchall()
    out = {}
    for slug in SCHOOLS:
        school = app.COLLEGES_BY_SLUG.get(slug)
        if not school:
            continue
        for r in rows:
            prof = dict(r)
            key = f"{slug}|u{prof['user_id']}"
            try:
                fit, _ = app.compute_fit(dict(prof), school)
                lo_hi = app.estimate_odds(school, fit, dict(prof))
                out[key] = json.dumps({"fit": fit, "odds": lo_hi}, sort_keys=True, default=str)
            except Exception as e:
                out[key] = "ERR:" + str(e)[:90]
    detail = app.ADMISSIONS_DETAIL.get("stanford", {})
    out["roundsplit|stanford"] = json.dumps(
        app.deterministic_round_odds(app.COLLEGES_BY_SLUG["stanford"], detail, 0.5),
        sort_keys=True, default=str)
    return out


def main():
    snap = snapshot()
    if "--write-baseline" in sys.argv:
        json.dump(snap, open(BASELINE, "w"), sort_keys=True, indent=1)
        print(f"baseline written: {len(snap)} entries")
        return 0
    if not os.path.exists(BASELINE):
        print("NO BASELINE — run with --write-baseline first")
        return 2
    base = json.load(open(BASELINE))
    drift = {k: (base.get(k), snap.get(k)) for k in set(base) | set(snap)
             if base.get(k) != snap.get(k)}
    if drift:
        print(f"ODDS DRIFT — {len(drift)} of {len(base)} entries changed:")
        for k, (b, a) in sorted(drift.items())[:10]:
            print(f"  {k}\n    was: {b}\n    now: {a}")
        return 1
    print(f"ODDS REGRESS PASS — {len(base)} entries identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
