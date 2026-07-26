#!/usr/bin/env python3
"""One-off: 'admissions are rigged' hot-take carousels (test batch for the 8am slot).

Four hot-take types, each a 4-slide carousel: title1 (the RIGGED hook, with school named)
-> title2 (the comparison numbers, computed live from the engine) -> profile -> chances
reveal. Only builds hot takes VERIFIED to produce a dramatic, true delta in the engine.
Run with --dry to preview. The 2 legacy ones (Harvard/Princeton) are already queued.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from factory import _shot, _title_png, _finish, start_local_server, stop_local_server, LOCAL_URL, CRON_KEY

BASE = dict(uw_gpa=3.9, weighted_gpa=4.3, sat=1500, sat_math=760, sat_ebrw=740,
            ecs="Varsity Golf\nModel UN\nInvestment Club (Founder)\nSummer Analyst Intern",
            leadership="Model UN Secretary-General, Investment Club Founder",
            awards="AP Scholar with Distinction, Regional Business Plan Winner",
            first_gen=0, athlete=0, is_exceptional=0, ec_rating=420)

def _od(sch, p):
    return app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
def _mid(o): return (o[0] + o[1]) / 2.0
def _mx(a, b): return round(_mid(a) / max(0.5, _mid(b)), 1)
def _x(m): return f"{int(round(m))}X" if abs(m - round(m)) < 0.15 else f"{m}X"

def build(ht, dry=False):
    slug = ht["slug"]; sch = app.COLLEGES_BY_SLUG[slug]
    short = app._school_brand(slug, sch["name"])[0].upper()
    st = sch.get("state", "")
    if ht["type"] == "legacy":
        p = dict(BASE, major="Economics", state="Connecticut", school_type="private",
                 legacy_schools=f"{sch['name']} 2x")
        pn = dict(p); pn["legacy_schools"] = ""
        o1, o0 = _od(sch, p), _od(sch, pn)
        m = _x(_mx(o1, o0))
        t1 = ([short + " ADMISSIONS", "ARE RIGGED"], ["RIGGED"])
        t2 = (["THIS APPLICANT HAD", f"{m} HIGHER ODDS", "FROM LEGACY"], [m, "LEGACY"])
        reveal = o1; save = p
    elif ht["type"] == "major":
        p = dict(BASE, major="Computer Science", state=st)   # in-state to isolate major
        pe = dict(p, major="English")
        ocs, oen = _od(sch, p), _od(sch, pe)
        t1 = (["YOUR MAJOR DECIDES", "YOUR ODDS AT", short], ["MAJOR"])
        t2 = ([f"CS: {ocs[0]}-{ocs[1]}%", "vs", f"ENGLISH: {oen[0]}-{oen[1]}%"], ["CS:", "ENGLISH:"])
        reveal = ocs; save = p
    elif ht["type"] == "residency":
        p = dict(BASE, major="Economics", state="Nevada")     # out-of-state kid
        pi = dict(p, state=st)                                 # same kid, in-state
        oos, ins = _od(sch, p), _od(sch, pi)
        m = _x(_mx(ins, oos))
        t1 = (["YOUR ZIP CODE", f"IS WORTH {m}", "AT " + short], [f"{m}"])
        t2 = ([f"IN-STATE: {ins[0]}-{ins[1]}%", "vs", f"OUT-OF-STATE: {oos[0]}-{oos[1]}%"], ["IN-STATE:"])
        reveal = oos; save = p
    elif ht["type"] == "ed":
        from candor.rounds import deterministic_round_odds
        p = dict(BASE, major="Economics", state=st)
        o = _od(sch, p); mid = _mid(o) / 100.0
        detail = {"rounds": ["ED", "RD"], "rates": {"ED": sch.get("accept", 0.1) * 2.4, "RD": sch.get("accept", 0.1)}}
        r = deterministic_round_odds(sch, detail, mid)
        ed_p, rd_p = round(r.get("ED", mid) * 100), round(r["RD"] * 100)
        m = _x(round(ed_p / max(1, rd_p), 1))
        t1 = (["APPLYING EARLY", f"= {m} THE ODDS", "AT " + short], [f"{m}"])
        t2 = ([f"EARLY: {ed_p}%", "vs", f"REGULAR: {rd_p}%"], ["EARLY:"])
        reveal = o; save = p
    else:
        raise ValueError(ht["type"])

    app.save_profile(181, save)
    print(f"{ht['type']:<10} {slug:<10} reveal {reveal[0]}-{reveal[1]}%  | {' '.join(t1[0])}")
    title1 = _title_png(slug, t1[0], t1[1])
    title2 = _title_png(slug, t2[0], t2[1])
    tmp = tempfile.mkdtemp(prefix="ht_")
    prof = _shot(f"{tmp}/p.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181")
    chances = _shot(f"{tmp}/c.png", f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1", static=True)
    payload = dict(school_slug=slug, school_name=sch["name"], accent=app._school_brand(slug, sch["name"])[1],
                   title_text=" ".join(t1[0]), title_formula=f"hottake-{ht['type']}", slide3_type="chances",
                   profile_json=json.dumps(save), odds_text=f"{reveal[0]}-{reveal[1]}%", grade_text="",
                   img1=title1, img2=title2, img3=prof, img4=chances,
                   meta={"hot_take": ht["type"], "lines": t1[0]})
    return _finish(payload, dry, f"HOTTAKE {ht['type']} {slug}")

HOTTAKES = [
    {"type": "legacy", "slug": "georgetown"},
    {"type": "major", "slug": "ut-austin"},
    {"type": "major", "slug": "uiuc"},
    {"type": "residency", "slug": "unc"},
    {"type": "residency", "slug": "ut-austin"},
    {"type": "ed", "slug": "tulane"},
]

def main():
    dry = "--dry" in sys.argv
    start_local_server()
    try:
        for ht in HOTTAKES:
            if ht["slug"] not in app.COLLEGES_BY_SLUG:
                print("skip (no school):", ht); continue
            try:
                build(ht, dry=dry)
            except Exception as e:
                print(f"  FAILED {ht}: {e}")
    finally:
        stop_local_server()

if __name__ == "__main__":
    main()
