#!/usr/bin/env python3
"""Daily RIGGED batch: one legacy-hot-take carousel PER ACCOUNT, rotating schools.

Born from the 2026-07-20 A/B test — Georgetown RIGGED hit 1,109 views (3.5x floor)
and Princeton 766 while regular posts sat at 270-330. Server-side, these release
sprinkled across the day (one per account, each at that account's rotating rigged
slot — see app._rigged_slot_today), NOT as a single wave.

School pool: app._LEGACY_MULT >= 1.8, minus ED-only schools (Duke/Cornell/etc. —
their OVERALL odds don't move, so the reveal slide would show no boost) and minus
schools any rigged carousel used in the last RECENT_DAYS days. Pushes at
priority=50 with title_formula 'daily-rigged' so the release gate can spot them.

Run daily (launchd com.candor.dailyrigged, ~6:40am) with --n <accounts> or default 8.
--dry to preview without pushing.
"""
import os, sys, json, tempfile, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from factory import (_shot, _title_png, _finish, start_local_server, stop_local_server,
                     LOCAL_URL, CRON_KEY, TARGET_URL)

RECENT_DAYS = 5

# Competitive-but-not-superstar applicant; 2-gen legacy is the visible differentiator.
def _profile(school_name):
    return dict(
        uw_gpa=3.9, weighted_gpa=4.3, sat=1500, sat_math=760, sat_ebrw=740,
        major="Economics", state="Connecticut", school_type="private",
        ecs="Varsity Golf\nModel UN\nInvestment Club (Founder)\nSummer Analyst Intern",
        leadership="Model UN Secretary-General, Investment Club Founder",
        awards="AP Scholar with Distinction, Regional Business Plan Winner",
        legacy_schools=f"{school_name} 2x",
        first_gen=0, athlete=0, is_exceptional=0, ec_rating=420,
    )


def _mid(o):
    return (o[0] + o[1]) / 2.0


def _pool():
    """Legacy schools worth a RIGGED post: mult >= 1.8, not ED-only, exists in COLLEGES."""
    out = []
    for slug, mult in sorted(app._LEGACY_MULT.items(), key=lambda kv: -kv[1]):
        if mult < 1.8 or slug in app._LEGACY_ED_ONLY:
            continue
        if slug in app.COLLEGES_BY_SLUG:
            out.append(slug)
    return out


def _recent_rigged_slugs():
    """Slugs used by any rigged/hot-take carousel recently (via live queue API)."""
    try:
        url = f"{TARGET_URL}/content/queue/recent-rigged?key={CRON_KEY}&days={RECENT_DAYS}"
        with urllib.request.urlopen(url, timeout=15) as r:
            return set(json.load(r).get("slugs", []))
    except Exception:
        return set()


def build(slug, dry=False):
    sch = app.COLLEGES_BY_SLUG[slug]
    name = sch["name"]
    p = _profile(name)
    p_no = dict(p); p_no["legacy_schools"] = ""
    o0 = app.estimate_odds_v2(sch, p_no) or app.estimate_odds(sch, app.compute_my_fit(p_no, sch), p_no)
    o1 = app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    mult = round(_mid(o1) / max(0.1, _mid(o0)), 1)
    if mult < 1.6:   # reveal wouldn't look dramatic — skip rather than post a dud
        print(f"  skip {slug}: live mult only {mult}x")
        return None
    mtxt = f"{int(round(mult))}X" if abs(mult - round(mult)) < 0.15 else f"{mult}X"
    short, accent = app._school_brand(slug, name)

    app.save_profile(181, p)
    t1 = [short.upper() + " ADMISSIONS", "ARE RIGGED"]
    t2 = ["THIS APPLICANT HAD", f"{mtxt} HIGHER ODDS", "BECAUSE OF LEGACY"]
    img1 = _title_png(slug, t1, ["RIGGED"])
    img2 = _title_png(slug, t2, [mtxt, "LEGACY"])
    tmp = tempfile.mkdtemp(prefix="drig_")
    prof = _shot(f"{tmp}/p.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181")
    chances = _shot(f"{tmp}/c.png", f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1", static=True)

    payload = dict(
        school_slug=slug, school_name=name, accent=accent,
        title_text=" ".join(t1), title_formula="daily-rigged", slide3_type="chances",
        profile_json=json.dumps(p), odds_text=f"{o1[0]}-{o1[1]}%", grade_text="",
        priority=50,
        img1=img1, img2=img2, img3=prof, img4=chances,
        meta={"hot_take": "legacy_rigged_daily", "legacy_mult": mtxt, "lines": t1},
    )
    return _finish(payload, dry, f"DAILY RIGGED {slug} ({mtxt})")


def main():
    dry = "--dry" in sys.argv
    n = 8
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    used = _recent_rigged_slugs()
    todo = [s for s in _pool() if s not in used][: n + 4]   # a few spares for skips
    if not todo:
        print("pool exhausted — everything used recently; widening")
        todo = _pool()[: n + 4]
    made = 0
    start_local_server()
    try:
        for slug in todo:
            if made >= n:
                break
            try:
                if build(slug, dry=dry):
                    made += 1
            except Exception as e:
                print(f"  FAILED {slug}: {e}")
    finally:
        stop_local_server()
    print(f"done: {made}/{n} rigged carousels" + (" (dry)" if dry else ""))


if __name__ == "__main__":
    main()
