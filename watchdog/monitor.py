#!/usr/bin/env python3
"""Candor watchdog — detector. launchd runs this every 2 minutes.

Checks the key public routes on the live site. A route must fail on TWO
consecutive runs to open an incident (no acting on one-off blips). While
green, records the deployed commit as "last known good" — that's the
rollback floor the fixer converges to. On incident open, hands off to
handler.py (the fix ladder) and texts Jasper at every state change.

State: watchdog/state.json  ·  Log: watchdog/incidents.log
Env (from ~/.candor_autopilot.env): PHONE, RAILWAY_API_TOKEN (for handler).
DRY_RUN=1 = never push/deploy, just diagnose + text.
"""
import json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "incidents.log")
BASE = os.environ.get("WATCHDOG_BASE", "https://candoradmit.com")
ROUTES = ["/", "/colleges", "/college/stanford", "/rankings", "/login",
          "/guides", "/healthz"]
REPO_URL = "https://github.com/jasperthelazzer19-cell/College-Admissions.git"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    json.dump(st, open(STATE, "w"), indent=1)


def check_route(path):
    """(ok, status) — one retry inside the run so a single dropped packet
    doesn't count as a fail."""
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(BASE + path,
                                         headers={"User-Agent": "candor-watchdog"})
            with urllib.request.urlopen(req, timeout=25) as r:
                if r.status < 500:
                    return True, r.status
                status = r.status
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True, e.code          # 3xx/4xx = route alive
            status = e.code
        except Exception:
            status = 0                        # timeout / connection refused
        if attempt == 1:
            time.sleep(4)
    return False, status


def origin_main_commit():
    try:
        out = subprocess.run(["git", "ls-remote", REPO_URL, "refs/heads/main"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.split()[0][:12] if out.stdout else None
    except Exception:
        return None


def main():
    if os.environ.get("WATCHDOG_FAKE_FAIL"):
        results = {p: (False, 500) for p in ROUTES}
    else:
        results = {p: check_route(p) for p in ROUTES}
    failing = {p: s for p, (ok, s) in results.items() if not ok}
    st = load_state()

    if not failing:
        st["pending"] = {}
        commit = origin_main_commit()
        if commit:
            st["last_green_commit"] = commit
        st["last_green_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if st.get("incident"):
            log(f"RECOVERED — clearing incident {st['incident'].get('open_at')}")
            try:
                from handler import send_text
                send_text("✅ Candor watchdog: site is healthy again.")
            except Exception:
                pass
            st["incident"] = None
        save_state(st)
        return

    # something failing — require 2 consecutive runs before acting
    pending = st.get("pending", {})
    confirmed = {p: s for p, s in failing.items() if p in pending}
    st["pending"] = {p: s for p, s in failing.items()}
    save_state(st)
    if not confirmed:
        log(f"first-strike (unconfirmed): {failing} — waiting for next run")
        return

    incident = st.get("incident")
    if incident and incident.get("frozen"):
        log(f"incident FROZEN, still failing: {confirmed} — human needed")
        return
    if incident and time.time() - incident.get("last_action_ts", 0) < 420:
        log("incident open, action cooldown — letting last fix settle")
        return

    log(f"CONFIRMED failures: {confirmed} — invoking handler")
    import handler
    handler.handle(confirmed, st)


if __name__ == "__main__":
    main()
