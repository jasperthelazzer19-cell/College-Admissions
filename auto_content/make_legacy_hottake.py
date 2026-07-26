#!/usr/bin/env python3
"""One-off: 'ADMISSIONS ARE RIGGED' legacy hot-take carousels for Harvard + Princeton.

Structure (4 slides): title1 "SCHOOL ADMISSIONS ARE RIGGED" -> title2 "this applicant
had ~Nx higher odds because of legacy" -> profile slide -> chances reveal (showing the
legacy-boosted odds the new engine now produces). Pushes to the live queue for the next
slot. Run with --dry to preview without pushing.
"""
import os, sys, json, urllib.parse, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import factory
from factory import _shot, _title_png, _finish, start_local_server, stop_local_server, LOCAL_URL, CRON_KEY

# A competitive-but-not-superstar applicant, 2-generation legacy — so legacy is the
# visible differentiator (the whole point of the hot take).
def _profile(school_name):
    return dict(
        uw_gpa=3.9, weighted_gpa=4.3, sat=1500, sat_math=760, sat_ebrw=740,
        major="Economics", state="Connecticut", school_type="private",
        ecs="Varsity Golf\nModel UN\nInvestment Club (Founder)\nSummer Analyst Intern",
        leadership="Model UN Secretary-General, Investment Club Founder",
        awards="AP Scholar with Distinction, Regional Business Plan Winner",
        legacy_schools=f"{school_name} 2x",   # both parents attended
        first_gen=0, athlete=0, is_exceptional=0, ec_rating=420,
    )

def _mid(o):  # midpoint of (low,high)
    return (o[0] + o[1]) / 2.0

def build(slug, dry=False):
    sch = app.COLLEGES_BY_SLUG[slug]
    name = sch["name"]
    p = _profile(name)
    # real multiplier: no-legacy vs legacy on the LIVE engine
    p_noleg = dict(p); p_noleg["legacy_schools"] = ""
    o0 = app.estimate_odds_v2(sch, p_noleg) or app.estimate_odds(sch, app.compute_my_fit(p_noleg, sch), p_noleg)
    o1 = app.estimate_odds_v2(sch, p)       or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    mult = round(_mid(o1) / max(0.1, _mid(o0)), 1)
    mult_txt = f"{int(round(mult))}X" if abs(mult - round(mult)) < 0.15 else f"{mult}X"
    print(f"{slug}: no-legacy {o0[0]}-{o0[1]}%  ->  legacy {o1[0]}-{o1[1]}%   = {mult_txt} higher")

    # persist the legacy profile to the demo user so the profile + chances exports render it
    app.save_profile(181, p)

    short, _ = app._school_brand(slug, name)
    t1_lines = [short.upper() + " ADMISSIONS", "ARE RIGGED"]
    t2_lines = ["THIS APPLICANT HAD", f"{mult_txt} HIGHER ODDS", "BECAUSE OF LEGACY"]
    title1 = _title_png(slug, t1_lines, ["RIGGED"])
    title2 = _title_png(slug, t2_lines, [mult_txt, "LEGACY"])
    tmp = tempfile.mkdtemp(prefix="leg_")
    prof = _shot(f"{tmp}/p.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181")
    chances = _shot(f"{tmp}/c.png", f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1", static=True)

    payload = dict(
        school_slug=slug, school_name=name, accent=app._school_brand(slug, name)[1],
        title_text=" ".join(t1_lines), title_formula="legacy-hottake", slide3_type="chances",
        profile_json=json.dumps(p), odds_text=f"{o1[0]}-{o1[1]}%", grade_text="",
        img1=title1, img2=title2, img3=prof, img4=chances,
        meta={"hot_take": "legacy_rigged", "legacy_mult": mult_txt, "lines": t1_lines},
    )
    return _finish(payload, dry, f"LEGACY RIGGED {slug} ({mult_txt})")

def main():
    dry = "--dry" in sys.argv
    start_local_server()
    try:
        for slug in ("harvard", "princeton"):
            build(slug, dry=dry)
    finally:
        stop_local_server()

if __name__ == "__main__":
    main()
