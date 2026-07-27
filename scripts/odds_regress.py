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


# FROZEN FIXTURES, not live rows.
#
# This used to read `SELECT * FROM profiles ... ORDER BY user_id LIMIT 6`, which
# picked up user 181 and 38 -- the CONTENT DEMO profiles that every carousel run
# overwrites (factory.py, make_celeb.py and make_daily_rigged.py all save_profile
# onto them). So generating a single carousel locally silently changed the gate's
# inputs, the next run "failed", and the failure meant nothing. It cried wolf
# twice on 2026-07-27 alone, which is exactly how a gate stops being read.
#
# These six are frozen copies spanning the range the engine has to get right:
# elite-tested, strong-untested, mid, weak, hooked and spiky. Editing one is a
# deliberate act that requires re-baselining -- which is the point.
FIXTURES = [
    dict(user_id="elite", uw_gpa=4.0, weighted_gpa=4.6, sat=1550, sat_math=790, sat_ebrw=760,
         major="Computer Science", state="California", school_type="private",
         aps="AP Calculus BC, AP Physics C, AP CS A, AP Chemistry, AP English Lang",
         ecs="Game Jam Founder ($100k+ sponsorships)\nGame Dev Club President\nResearch Intern",
         leadership="Club President", awards="National Merit Finalist",
         legacy_schools="", first_gen=0, athlete=0, is_exceptional=0, ec_rating=640.0),
    dict(user_id="strong", uw_gpa=3.9, weighted_gpa=4.3, sat=1500, sat_math=760, sat_ebrw=740,
         major="Economics", state="Connecticut", school_type="private",
         aps="AP English Literature, AP US History, AP Calculus AB, AP Biology",
         ecs="Varsity Golf\nModel UN\nInvestment Club (Founder)\nSummer Analyst Intern",
         leadership="Model UN Secretary-General", awards="AP Scholar with Distinction",
         legacy_schools="", first_gen=0, athlete=0, is_exceptional=0, ec_rating=510.0),
    dict(user_id="notest", uw_gpa=3.8, weighted_gpa=4.1, sat=None, act=None,
         major="Art History", state="New York", school_type="public", aps="AP Art History",
         ecs="Museum Educator (3 years), Photography Fellow", leadership="",
         awards="", legacy_schools="", first_gen=0, athlete=0, is_exceptional=0, ec_rating=340.0),
    dict(user_id="service", uw_gpa=4.0, weighted_gpa=4.2, sat=None, act=31,
         major="Sociology", state="Illinois", school_type="public", aps="AP Psychology",
         ecs="Youth Service Club President (3 years), GSA Co-lead", leadership="President",
         awards="", legacy_schools="", first_gen=1, athlete=0, is_exceptional=0, ec_rating=380.0),
    dict(user_id="mid", uw_gpa=3.5, weighted_gpa=3.8, sat=1280, sat_math=650, sat_ebrw=630,
         major="Biology", state="Texas", school_type="public", aps="AP Biology",
         ecs="Varsity Soccer (4 years)\nPart-time job", leadership="Team captain",
         awards="Honor Roll", legacy_schools="", first_gen=0, athlete=0,
         is_exceptional=0, ec_rating=240.0),
    dict(user_id="spike", uw_gpa=3.7, weighted_gpa=4.1, sat=1450, sat_math=760, sat_ebrw=690,
         major="Mathematics", state="Massachusetts", school_type="private",
         aps="AP Calculus BC, AP Physics C",
         ecs="USAMO qualifier\nRSI alum\nMath team captain", leadership="Captain",
         awards="USAMO qualifier, ISEF finalist", legacy_schools="", first_gen=0,
         athlete=0, is_exceptional=1, ec_rating=940.0),
]


def snapshot():
    rows = FIXTURES
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
