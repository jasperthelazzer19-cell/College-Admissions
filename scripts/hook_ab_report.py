#!/usr/bin/env python3
"""Hook A/B readout — per-arm TikTok performance for the 2026-07-26 hook test.

READ-ONLY. Opens the DB in immutable mode and never writes, so it's safe to point
at a copy pulled off prod (or at the local college.db).

    python3 scripts/hook_ab_report.py                    # ./college.db
    DB_PATH=/tmp/prod-copy.db python3 scripts/hook_ab_report.py
    python3 scripts/hook_ab_report.py --since 2026-07-26 # ignore pre-test posts

Arms are read from app.HOOK_AB_ARMS so this can never disagree with what's
actually shipping. A carousel is filed under an arm by meta.hook_arm (stamped at
push time in content_queue_push); rows with no tag fall back to the arm of their
assigned/posted account, which is how anything pushed before the tagging landed
still gets counted. Untagged, unattributable rows are reported separately rather
than silently folded into an arm.

Only ATTRIBUTED posts count — content_queue JOIN tiktok_posts ON carousel_id.
That join is the same one tiktok_perf_by() uses, so these numbers are directly
comparable to the format/school breakdowns on the dashboard.
"""
import os, sys, json, sqlite3, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB_PATH = os.environ.get("DB_PATH") or os.path.join(ROOT, "college.db")
SINCE = None
if "--since" in sys.argv:
    SINCE = sys.argv[sys.argv.index("--since") + 1]

# Pull the arm definition from app itself. Importing app is heavy but it's the
# only way to guarantee the report and the live splitter agree; a hard-coded copy
# here would rot the first time the arms are edited.
os.environ.pop("ANTHROPIC_KEY", None)          # never bill an LLM from a report
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["DB_PATH"] = DB_PATH
try:
    import app
    ARMS = dict(app.HOOK_AB_ARMS)
except Exception as e:                          # app import failed -> still usable
    print(f"(couldn't import app: {e} — falling back to the arms hard-coded below)")
    ARMS = {"A": ("@candor54", "@candoradmit", "Candoradmitt", "@candor20"),
            "B": ("candoradmit19", "@candoradmit.com3", "@candoradmit.com", "Candor57")}
ARM_OF_LABEL = {lab: arm for arm, labs in ARMS.items() for lab in labs}


def rows():
    """Attributed posts joined to their carousel. Read-only connection."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT c.id, c.meta, c.assigned_account, c.posted_account, c.slide3_type, "
           "       c.title_formula, c.title_text, c.released_at, "
           "       p.view_count v, p.like_count l, p.comment_count cm, p.share_count sh, "
           "       p.create_time ct "
           "FROM content_queue c JOIN tiktok_posts p ON p.carousel_id = c.id "
           "WHERE p.view_count IS NOT NULL")
    args = []
    if SINCE:
        # create_time is a unix ts; compare in SQL so we don't pull the whole table.
        sql += " AND p.create_time >= strftime('%s', ?)"
        args.append(SINCE)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def arm_of(r):
    """meta.hook_arm first (durable — the 'no account' release path NULLs
    assigned_account), then the account the row is tied to."""
    try:
        if r["meta"]:
            a = (json.loads(r["meta"]) or {}).get("hook_arm")
            if a in ARMS:
                return a
    except Exception:
        pass
    for col in ("assigned_account", "posted_account"):
        a = ARM_OF_LABEL.get((r[col] or "").strip())
        if a:
            return a
    return None


def _avg(xs):
    return (sum(xs) / len(xs)) if xs else 0.0


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def summarize(label, rs):
    v = [float(r["v"] or 0) for r in rs]
    l = [float(r["l"] or 0) for r in rs]
    c = [float(r["cm"] or 0) for r in rs]
    s = [float(r["sh"] or 0) for r in rs]
    print(f"\n  {label:<26} n={len(rs)}")
    if not rs:
        return
    # MEDIAN views alongside the mean on purpose: view counts are long-tailed, so
    # one lucky post can move an arm's average by 30% while the typical post is
    # unchanged. If mean and median disagree, trust the median.
    print(f"    views      avg {_avg(v):8.1f}   median {_median(v):8.1f}   total {sum(v):9.0f}")
    print(f"    likes      avg {_avg(l):8.2f}   per 1k views {1000*sum(l)/max(1.0, sum(v)):6.2f}")
    print(f"    comments   avg {_avg(c):8.3f}   per 1k views {1000*sum(c)/max(1.0, sum(v)):6.2f}")
    print(f"    shares     avg {_avg(s):8.3f}   per 1k views {1000*sum(s)/max(1.0, sum(v)):6.2f}")


def main():
    if not os.path.exists(DB_PATH):
        print(f"no DB at {DB_PATH} (set DB_PATH)"); sys.exit(1)
    rs = rows()
    print(f"DB: {DB_PATH}")
    print(f"attributed posts: {len(rs)}" + (f"  (since {SINCE})" if SINCE else ""))
    if not rs:
        print("nothing to report — no carousels have been attributed to a TikTok post yet.")
        return

    by_arm = collections.defaultdict(list)
    for r in rs:
        by_arm[arm_of(r)].append(r)

    print("\n" + "=" * 64)
    print("PER-ARM  (A = new hooks, B = control)")
    print("=" * 64)
    for arm in ("A", "B"):
        summarize(f"arm {arm}", by_arm.get(arm, []))
    if by_arm.get(None):
        # Dormant accounts, hand-posted one-offs, and everything from before the
        # test started. Deliberately NOT folded into either arm.
        summarize("unassigned / pre-test", by_arm[None])

    # Format split inside each arm. If one arm drew far more h2h than the other,
    # a difference in views might be the format, not the hook — check this before
    # calling a winner.
    print("\n" + "=" * 64)
    print("FORMAT MIX PER ARM (sanity check — the arms should look alike here)")
    print("=" * 64)
    for arm in ("A", "B"):
        mix = collections.Counter((r["slide3_type"] or "?") for r in by_arm.get(arm, []))
        tot = max(1, sum(mix.values()))
        print(f"  arm {arm}: " + ", ".join(f"{k} {n} ({100*n/tot:.0f}%)"
                                           for k, n in mix.most_common()))

    # Best/worst individual hooks in the new arm — the point of a 2-week run is
    # not just "did A win" but "which A hooks are worth keeping".
    a_rows = sorted(by_arm.get("A", []), key=lambda r: -(r["v"] or 0))
    if a_rows:
        print("\n" + "=" * 64)
        print("ARM A — TOP 10 BY VIEWS (which new hooks actually landed)")
        print("=" * 64)
        for r in a_rows[:10]:
            print(f"  {int(r['v'] or 0):>7} views  {int(r['l'] or 0):>4}L {int(r['cm'] or 0):>3}C  "
                  f"#{r['id']} {(r['title_text'] or '')[:58]}")
        print("\n  BOTTOM 5:")
        for r in a_rows[-5:]:
            print(f"  {int(r['v'] or 0):>7} views  {int(r['l'] or 0):>4}L {int(r['cm'] or 0):>3}C  "
                  f"#{r['id']} {(r['title_text'] or '')[:58]}")

    # Crude read on whether the gap is real. Not a t-test — with n in the low
    # hundreds and a long tail, a 10% gap is noise. Treat <20% as "no result".
    va, vb = [float(r["v"] or 0) for r in by_arm.get("A", [])], [float(r["v"] or 0) for r in by_arm.get("B", [])]
    if len(va) >= 20 and len(vb) >= 20:
        ma, mb = _median(va), _median(vb)
        gap = 100 * (ma - mb) / mb if mb else 0
        print("\n" + "=" * 64)
        verdict = ("A wins" if gap >= 20 else "B wins" if gap <= -20 else "NO RESULT (inside the noise)")
        print(f"median views  A {ma:.0f}  vs  B {mb:.0f}   ->  {gap:+.0f}%   {verdict}")
        print("=" * 64)
    else:
        print(f"\n(too few attributed posts to call it: A={len(va)}, B={len(vb)}; want 20+ each)")


if __name__ == "__main__":
    main()
