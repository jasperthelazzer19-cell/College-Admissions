#!/usr/bin/env python3
"""Candor watchdog — the fix ladder. Called by monitor.py on a CONFIRMED outage.

Rungs, in order (each attempt is logged + texted):
  0. INFRA    — site down but no code shipped since last green and no app
                traceback in logs -> Railway/network problem. Text + wait.
                Deploy-spamming an infra outage makes it worse (learned 6/30).
  1. ROLLBACK — commits landed since last green -> revert them in a DEDICATED
                clone (never Jasper's working tree) and push. Converges by
                definition: last green commit was green.
  2. CLAUDE   — traceback with no recent commit (latent bug), or revert
                conflicted -> headless `claude -p` (Max plan, $0) writes a
                minimal fix in the clone; the smoke gate must pass before push.
  3. FREEZE   — 3 actions without recovery -> stop, text for a human.

Hard rules baked in: never touches the DB or data, never edits odds/grading/
payment logic, minimal diffs only, every deploy gated on scripts/smoke.py.
DRY_RUN=1 -> diagnose + text, no push."""
import json, os, re, subprocess, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_CHECKOUT = os.path.dirname(HERE)                 # ~/college-tool
CLONE = os.path.join(HERE, "repo")
REPO_URL = "https://github.com/jasperthelazzer19-cell/College-Admissions.git"
BASE = os.environ.get("WATCHDOG_BASE", "https://candoradmit.com")
DRY = os.environ.get("DRY_RUN") == "1"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or os.path.expanduser(
    "~/.nvm/versions/node/v25.9.0/bin/claude")
MAX_ATTEMPTS = 3

from monitor import log, save_state, origin_main_commit, check_route  # noqa: E402


def send_text(msg):
    num = os.environ.get("PHONE", "")
    if not num:
        log("PHONE not set — can't text"); return
    esc = str(msg).replace("\\", "\\\\").replace('"', '\\"')
    script = ('tell application "Messages"\n set svc to 1st service whose '
              f'service type = iMessage\n set b to buddy "{num}" of svc\n'
              f' send "{esc}" to b\nend tell')
    try:
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, timeout=45)
    except Exception as e:
        log(f"text failed: {e}")


def _run(cmd, cwd=None, timeout=120, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def railway_logs_tail():
    """Recent app logs via the railway CLI (linked in the main checkout)."""
    try:
        r = _run(["railway", "logs"], cwd=MAIN_CHECKOUT, timeout=60)
        return (r.stdout or "")[-8000:]
    except Exception as e:
        return f"(railway logs failed: {e})"


def extract_traceback(logs):
    blocks = re.findall(r"Traceback[\s\S]{0,1500}?(?:Error|Exception)[^\n]*", logs)
    return blocks[-1] if blocks else ""


def ensure_clone():
    if not os.path.isdir(os.path.join(CLONE, ".git")):
        _run(["git", "clone", REPO_URL, CLONE], timeout=300)
    _run(["git", "fetch", "origin"], cwd=CLONE)
    _run(["git", "checkout", "main"], cwd=CLONE)
    _run(["git", "reset", "--hard", "origin/main"], cwd=CLONE)
    _run(["git", "clean", "-fd"], cwd=CLONE)


def smoke_gate():
    """Run the smoke suite against the CLONE's code (main checkout's DB)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_KEY", None); env.pop("ANTHROPIC_API_KEY", None)
    env["PYTHONPATH"] = CLONE
    env["DB_PATH"] = os.path.join(MAIN_CHECKOUT, "college.db")
    r = _run(["python3", os.path.join(CLONE, "scripts", "smoke.py")],
             cwd=CLONE, timeout=300, env=env)
    log(f"smoke gate: {'PASS' if r.returncode == 0 else 'FAIL'}\n{r.stdout[-800:]}")
    return r.returncode == 0


def push_clone():
    r = _run(["git", "push", "origin", "main"], cwd=CLONE, timeout=120)
    return r.returncode == 0, (r.stderr or r.stdout)[-300:]


def wait_recovery(paths, minutes=8):
    """Poll the live site until the failing routes recover (deploy included)."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(30)
        if all(check_route(p)[0] for p in paths):
            return True
    return False


def rung_rollback(st, incident, failing):
    last_green = st.get("last_green_commit")
    ensure_clone()
    head = _run(["git", "rev-parse", "HEAD"], cwd=CLONE).stdout.strip()[:12]
    if not last_green or head.startswith(last_green):
        return None                                  # nothing to roll back
    r = _run(["git", "revert", "--no-edit", f"{last_green}..HEAD"], cwd=CLONE)
    if r.returncode != 0:
        _run(["git", "revert", "--abort"], cwd=CLONE)
        log("revert conflicted — escalating to claude rung")
        return None
    if not smoke_gate():
        log("rollback failed the smoke gate?! freezing")
        return False
    if DRY:
        log("DRY_RUN: would push rollback"); return True
    ok, out = push_clone()
    if not ok:
        log(f"rollback push failed: {out}"); return False
    send_text(f"🛠 Candor watchdog: site was down ({', '.join(failing)}). "
              f"Auto-rolled back {last_green}..{head} — verifying…")
    if wait_recovery(list(failing)):
        send_text("✅ Watchdog: rollback worked, site recovered. The reverted "
                  "commits are still in history — refix at leisure.")
        return True
    return False


def rung_claude(incident, failing, traceback_txt):
    ensure_clone()
    prompt = f"""You are Candor's on-call fixer. The live site is failing.

FAILING ROUTES: {', '.join(failing)}
TRACEBACK FROM PRODUCTION LOGS:
{traceback_txt or '(none captured — reproduce via the smoke suite)'}

You are in a clean checkout of the repo. Fix the failure with the SMALLEST
possible diff. HARD RULES:
- Never modify the database, migrations, or any data.
- Never change odds/fit/grading/payment logic or numbers.
- Never touch env handling or secrets.
- After editing, run: PYTHONPATH=. DB_PATH={MAIN_CHECKOUT}/college.db python3 scripts/smoke.py
  and iterate until it passes.
- Commit with message 'watchdog: fix <short description>'. Do NOT push."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_KEY", None); env.pop("ANTHROPIC_API_KEY", None)
    log("invoking claude -p fixer (Max plan)…")
    if DRY:
        log("DRY_RUN: would run claude fixer"); return True
    try:
        r = _run([CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
                 cwd=CLONE, timeout=1500, env=env)
        log(f"claude fixer output tail: {(r.stdout or '')[-500:]}")
    except Exception as e:
        log(f"claude fixer errored: {e}"); return False
    committed = _run(["git", "log", "origin/main..HEAD", "--oneline"], cwd=CLONE).stdout.strip()
    if not committed:
        log("claude made no commit"); return False
    if not smoke_gate():
        log("claude fix failed the smoke gate — discarding")
        _run(["git", "reset", "--hard", "origin/main"], cwd=CLONE)
        return False
    ok, out = push_clone()
    if not ok:
        log(f"claude fix push failed: {out}"); return False
    send_text(f"🛠 Watchdog: wrote an auto-fix for {', '.join(failing)} "
              f"({committed.splitlines()[0]}), smoke tests passed, deploying…")
    if wait_recovery(list(failing)):
        send_text("✅ Watchdog: auto-fix deployed, site recovered.")
        return True
    return False


def handle(failing, st):
    incident = st.get("incident") or {"open_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                      "attempts": 0, "rungs": []}
    st["incident"] = incident
    incident["last_action_ts"] = time.time()
    logs = railway_logs_tail()
    tb = extract_traceback(logs)
    current = origin_main_commit()
    code_changed = bool(current and st.get("last_green_commit")
                        and current != st.get("last_green_commit"))

    if incident["attempts"] == 0:
        send_text(f"🚨 Candor watchdog: {', '.join(failing)} failing on prod. "
                  f"{'New commits since last green — trying rollback.' if code_changed else 'No new code — diagnosing.'}")

    # Rung 0: infra — no code change and no app traceback -> not fixable by deploys
    if not code_changed and not tb:
        if not incident.get("infra_texted"):
            send_text("⚠️ Watchdog: site down but no code changed and no app "
                      "error in logs — looks like Railway/network. Monitoring; "
                      "not deploying (that makes infra outages worse).")
            incident["infra_texted"] = True
        incident["type"] = "infra"
        save_state(st); return

    incident["attempts"] += 1
    if incident["attempts"] > MAX_ATTEMPTS:
        incident["frozen"] = True
        save_state(st)
        send_text(f"🧊 Watchdog FROZEN after {MAX_ATTEMPTS} attempts — "
                  f"{', '.join(failing)} still failing. Needs you. "
                  f"Last traceback: {tb[:220]}")
        return

    try:
        if code_changed and "rollback" not in incident["rungs"]:
            incident["rungs"].append("rollback")
            save_state(st)
            result = rung_rollback(st, incident, failing)
            if result is True:
                save_state(st); return
        incident["rungs"].append("claude")
        save_state(st)
        if rung_claude(incident, failing, tb):
            save_state(st); return
    except Exception as e:
        log(f"handler exception: {e}")
    save_state(st)
    log("action did not recover the site — next monitor cycle escalates")
