#!/usr/bin/env python3
"""One-shot slide-file prune for the Railway volume.

The app prunes slides automatically on boot (see _prune_slide_files in app.py),
but that only starts helping from the day it ships — this script is how you
reclaim the BACKLOG that accumulated before it existed (3.5GB of a 4.9GB volume
as of Jul 2026).

DRY BY DEFAULT. It reports what it *would* delete and exits; you have to pass
--apply to actually unlink anything.

    PYTHONPATH=. python3 scripts/prune_slides.py                 # report only
    PYTHONPATH=. python3 scripts/prune_slides.py --days 30       # be extra safe
    PYTHONPATH=. python3 scripts/prune_slides.py --apply         # do it

On the Railway box, DB_PATH must point at the volume copy so the retention
lookup reads the same content_queue the app does:

    DB_PATH=/app/data/college.db PYTHONPATH=. python3 scripts/prune_slides.py

The deletion policy lives ENTIRELY in app._prune_slide_files — this file is a
CLI wrapper on purpose, so the manual run and the automatic boot run can never
drift apart and start disagreeing about what is safe to remove.
"""
import os, sys

os.environ.pop("ANTHROPIC_KEY", None)      # importing app must never bill or
os.environ.pop("ANTHROPIC_API_KEY", None)  # block on an LLM call
# init_db() prunes on import. Suppress it, or a `--dry` run would silently delete
# for real before printing a word — the report must be the only thing that runs
# unless the operator passed --apply.
os.environ["SLIDE_PRUNE_SKIP_BOOT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def main():
    argv = sys.argv[1:]
    apply = "--apply" in argv
    days = app.SLIDE_RETENTION_DAYS
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("--days needs an integer")
            return 2
    if "--dry" in argv and apply:
        print("--dry and --apply are contradictory; refusing")
        return 2

    d = app._SLIDES_DIR
    total_files = total_bytes = 0
    try:
        for e in os.scandir(d):
            try:
                if e.is_file(follow_symlinks=False):
                    total_files += 1
                    total_bytes += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    except OSError as ex:
        print(f"no slides dir at {d} ({ex}) — nothing to do")
        return 0

    print(f"slides dir : {d}")
    print(f"db         : {app.DB_PATH}")
    print(f"on disk    : {total_files} files, {total_bytes / 1048576:.1f} MB")
    print(f"retention  : {days} days")
    print(f"mode       : {'APPLY (deleting)' if apply else 'DRY RUN (nothing deleted)'}")

    n, nbytes = app._prune_slide_files(days=days, dry=not apply)
    verb = "deleted" if apply else "would del"
    print(f"{verb}  : {n} files, {nbytes / 1048576:.1f} MB")
    left = total_bytes - nbytes
    print(f"remaining  : {total_files - n} files, {left / 1048576:.1f} MB")
    if not apply and n:
        print("\nRe-run with --apply to actually reclaim it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
