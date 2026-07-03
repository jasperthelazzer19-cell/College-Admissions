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
    if bad:
        print(f"SMOKE FAIL: {bad}")
        sys.exit(1)
    print("SMOKE PASS")

if __name__ == "__main__":
    main()
