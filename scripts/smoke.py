#!/usr/bin/env python3
"""Candor smoke gate: every key route must render without a 5xx.

Run against a checkout (PYTHONPATH=<repo> python3 scripts/smoke.py).
Used by the watchdog as the merge gate before any auto-fix deploys, and
runnable by hand before a risky push. Exits non-zero on any failure —
this exact suite would have caught the 2026-07 /colleges NameError."""
import os, sys

os.environ.pop("ANTHROPIC_KEY", None)       # never bill / never block on LLMs
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "college.db"))

import app  # noqa: E402

PUBLIC = ["/", "/colleges", "/colleges?q=harvard", "/college/stanford",
          "/college/harvard", "/college/tulane", "/rankings",
          "/rankings/my-fit", "/guides", "/login", "/signup", "/deadlines",
          "/healthz", "/sitemap.xml"]
AUTHED = ["/college/stanford/plan", "/chances/stanford", "/profile",
          "/plans", "/grade", "/improve"]

def main():
    cl = app.app.test_client()
    bad = []
    for path in PUBLIC:
        try:
            r = cl.get(path, follow_redirects=False)
            ok = r.status_code < 500
        except Exception as e:
            ok, r = False, None
            print(f"  EXC  {path}: {e}")
        if r is not None:
            print(f"  {r.status_code}  {path}")
        if not ok:
            bad.append(path)
    with cl.session_transaction() as s:
        s["user_id"] = 181                   # demo account
    for path in AUTHED:
        try:
            r = cl.get(path, follow_redirects=False)
            ok = r.status_code < 500
        except Exception as e:
            ok, r = False, None
            print(f"  EXC  {path} (authed): {e}")
        if r is not None:
            print(f"  {r.status_code}  {path} (authed)")
        if not ok:
            bad.append(path)
    bad += check_stat_guard()
    bad += check_slide_render()
    if bad:
        print(f"SMOKE FAIL: {bad}")
        sys.exit(1)
    print("SMOKE PASS")


# ── Invented-stat guard ───────────────────────────────────────────────────
# This exists because the ORIGINAL guard was silently inert for weeks: it caught
# 0 of 5 realistic hallucinations and nothing ever tested it, so wrong numbers
# shipped to TikTok and to the live site. A guard with no test is a guard that
# rots. These cases are pure functions — no LLM, no network, no cost.
_PROMPT = ("Duke: acceptance 6.3%, SAT mid-50% 1500-1570, ACT 34-35, "
           "GPA midpoint 3.95. Applicant SAT 1,540, GPA 3.94, 12 APs. Odds: 8-14%.")
_MUST_FLAG = [
    ("invented admit rate",   "Only 4% of engineering applicants get in"),
    ("spelled-out percent",   "Just 4 percent of applicants are admitted"),
    ("invented class rank",   "Outside the top 10% of your class"),
    ("invented ACT",          "ACT 29 is below Duke's range"),
    ("precision not given",   "At 6.28% acceptance it's brutal"),
    ("invented count",        "Beats 9,000 applicants in the pool"),
    ("invented SAT target",   "Push to 1720 and you're safe"),
]
_MUST_PASS = [
    ("rounded admit rate",    "At 6% acceptance this is a reach"),
    ("exact admit rate",      "At 6.3% acceptance this is a reach"),
    ("odds we supplied",      "Odds land 8-14% here"),
    ("SAT written w/ comma",  "SAT 1540 sits in the 1500-1570 band"),
    ("rounded GPA",           "A 3.9 GPA is just under the midpoint"),
    ("AP count",              "12 APs shows real rigor"),
    ("quartile vocabulary",   "Above the 75th percentile of admits"),
]


def check_stat_guard():
    failures = []
    for label, text in _MUST_FLAG:
        if not app._bad_stat_nums(text, _PROMPT):
            print(f"  MISS  stat-guard let through [{label}]: {text}")
            failures.append(f"stat-guard-miss:{label}")
    for label, text in _MUST_PASS:
        hit = app._bad_stat_nums(text, _PROMPT)
        if hit:
            print(f"  FALSE+ stat-guard wrongly flagged {hit} [{label}]: {text}")
            failures.append(f"stat-guard-false-positive:{label}")
    # The student-level "test-blind" mislabel that produced
    # "UChicago test-blind. SAT doesn't matter. Skip the SAT prep for this school."
    if app._test_label({"sat": None, "act": 35}) != "35 ACT":
        print("  FAIL  _test_label drops ACT-only scores")
        failures.append("test-label-drops-act")
    if "blind" in app._test_label({"sat": None, "act": None}).lower():
        print("  FAIL  _test_label calls a student 'test-blind' (reads as school policy)")
        failures.append("test-label-says-test-blind")
    if not failures:
        print(f"  OK   stat guard: {len(_MUST_FLAG)} caught, {len(_MUST_PASS)} passed clean")
    return failures


# ── Slide-render identity ─────────────────────────────────────────────────
# This exists because the LAST fix for mismatched slides only landed on half the
# path. factory.py was given a worker pool ([181, 9101..9105]) so parallel
# generations wouldn't clobber each other's demo profile, and its comment says so
# — but /profile/<slug>/export still read `38 if uid=="38" else 181`, so workers
# 9101-9105 rendered a stats slide belonging to whichever student worker 0 wrote
# last, while the plan slide (which DID honour uid) showed the right one. Five of
# every six carousels shipped two different applicants stapled together. Nothing
# tested the route, so the half-fix read as a whole one for weeks.
def check_slide_render():
    failures = []
    with app.app.test_request_context("/profile/stanford/export?uid=9103"):
        if app._render_uid() != 9103:
            print("  FAIL  _render_uid drops worker uids -> slide 2 renders the wrong student")
            failures.append("render-uid-drops-worker")
    with app.app.test_request_context("/profile/stanford/export?uid=38"):
        if app._render_uid() != 38:
            print("  FAIL  _render_uid drops Student B (h2h slide 3)")
            failures.append("render-uid-drops-38")
    for bad_uid in ("1", "9999", "abc", ""):
        with app.app.test_request_context(f"/profile/stanford/export?uid={bad_uid}"):
            if app._render_uid() != 181:
                print(f"  FAIL  _render_uid leaked a non-demo profile for uid={bad_uid!r}")
                failures.append(f"render-uid-leak:{bad_uid}")
    with app.app.test_request_context("/profile/stanford/export"):
        if app._render_uid() != 181:
            print("  FAIL  _render_uid default is not 181")
            failures.append("render-uid-default")
    # AP count was the number of commas in a prose sentence, so a profile saying
    # "9 AP courses; senior year: A, B, C, D, E" rendered as "5 APs".
    for text, want in [("9 AP courses; senior year: Calculus BC, Literature, Government, "
                        "Environmental Science, Computer Science Principles", 9),
                       ("11 AP exams (all 4s & 5s)", 11),
                       ("2 APs (Biology, Literature)", 2),
                       ("AP Physics, AP Calculus BC, AP Macroeconomics (4), AP Microeconomics (4)", 4),
                       ("", 0), (None, 0)]:
        got = app._ap_count(text)
        if got != want:
            print(f"  FAIL  _ap_count({text!r:.50}) = {got}, want {want}")
            failures.append("ap-count")
    if not failures:
        print("  OK   slide render: uid honoured for workers, non-demo uids refused, AP count parsed")
    return failures


if __name__ == "__main__":
    main()
