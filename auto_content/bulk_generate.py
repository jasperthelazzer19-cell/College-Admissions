#!/usr/bin/env python3
"""One-off BULK carousel generator for the coast/manual-schedule workflow.

Fills the pending buffer with N *varied* carousels in one long run, so the
creator can pull them out (Today → "Let N out (no send)") and schedule 1-2
weeks of posts by hand. Distinct from the scheduled autopilot in two ways:

  1. count_toward_cap=False — ignores SCHOOL_DAILY_CAP so a big batch keeps full
     school/format variety instead of collapsing into repeated 'compare' cards
     once every school hits its 2/day cap.
  2. No per-carousel texting — one summary line at the end, not 180 iMessages.

Runs on the Mac autopilot env (USE_MAX_CLI=1 → copy AND grading via free Max
CLI), so the whole run costs $0 on the API. Everything lands as status='pending';
nothing is sent to TikTok here. Auto-posting (if enabled) draws from the same
pending pool exactly as normal.

Usage (from ~/college-tool, autopilot env sourced):
    python3 auto_content/bulk_generate.py --n 180 [--workers 4]

Progress is appended to auto_content/bulk_generate.log.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import factory  # noqa: E402  (reuses server/lock/make_one/buffer_pending)
from factory import (make_one, buffer_pending, start_local_server,  # noqa: E402
                     wait_local_app, stop_local_server, render_lock)

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulk_generate.log")


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 180
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    workers = max(1, min(6, workers))
    # Refuse to run without the Max CLI, or this bulk run would silently bill the
    # API key for every one of the N carousels.
    if os.environ.get("USE_MAX_CLI") != "1":
        _log("REFUSING: USE_MAX_CLI!=1 — would bill the API. Source ~/.candor_autopilot.env first.")
        sys.exit(1)
    if not os.environ.get("CRON_KEY"):
        _log("REFUSING: CRON_KEY not set."); sys.exit(1)

    uid_pool = [181, 9101, 9102, 9103, 9104, 9105][:workers]
    start = buffer_pending()
    _log(f"=== BULK START: target +{n} carousels, {workers} workers, buffer now {start} ===")

    done = {"ok": 0, "fail": 0}
    with render_lock():                 # serialize with the text/button listener
        start_local_server()
        try:
            if not wait_local_app():
                _log("FATAL: local render server did not come up"); sys.exit(2)

            def _task(i):
                for attempt in range(3):    # transient SQLite contention clears on retry
                    try:
                        cid, name = make_one(dry=False, count_toward_cap=False,
                                             uid=uid_pool[i % workers])
                        if cid:
                            done["ok"] += 1
                            if done["ok"] % 10 == 0:
                                _log(f"  progress: {done['ok']}/{n} made "
                                     f"(buffer {buffer_pending()})")
                            return 1
                    except Exception as e:
                        _log(f"  carousel {i} attempt {attempt} failed: {e}")
                        time.sleep(3)
                done["fail"] += 1
                return 0

            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_task, range(n)))
        finally:
            stop_local_server()

    end = buffer_pending()
    _log(f"=== BULK DONE: {done['ok']} made, {done['fail']} failed. "
         f"buffer {start} -> {end} ===")


if __name__ == "__main__":
    main()
