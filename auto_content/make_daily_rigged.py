#!/usr/bin/env python3
"""Daily HOT-TAKE batch: one controversial carousel PER ACCOUNT, rotating angles.

Born from the 2026-07-20 A/B test — Georgetown RIGGED hit 1,109 views (3.5x floor)
and Princeton 766 while regular posts sat at 270-330. Server-side, these release
sprinkled across the day (one per account, each at that account's rotating rigged
slot — see app._rigged_slot_today), NOT as a single wave.

CAVEAT ON THAT ORIGIN STORY: the 2026-07-26 audit re-ran it on 29 attributed
rigged posts and got 357 avg views vs 340 for everything else. The 3.5x came from
a 2-post sample and did not survive. The format is kept because a hot take is
still the right shape, but it is not the proven winner the original note claimed.

ANGLES (2026-07-26). The batch used to be legacy-only, which capped it at 20
eligible schools — with 8/day and a 5-day no-repeat window it exhausted the pool
every 2.5 days and started repeating. Three angles now rotate, and each is backed
by a number the odds engine actually computes rather than a claim we invented:

  legacy  app._LEGACY_MULT — the original. Excludes ED-only-legacy schools, whose
          OVERALL odds don't move (the boost lives in the Early round), so the
          reveal would show nothing.
  round   candor.rounds.deterministic_round_odds — ED/REA vs RD, from the table
          calibrated on 2,350 real stored round-odds. This is the strongest angle
          and it UNLOCKS exactly the schools legacy had to skip: Columbia 2.7x,
          Duke 2.4x, Cornell 2.3x, Dartmouth 2.3x, Vanderbilt 2.3x.
  major   same profile, same stats, different intended major. Real swings at the
          schools that admit by college/school rather than university-wide
          (JHU 1.8x, Vanderbilt 1.75x, Northwestern 1.4x).

first-gen was tested as a fourth angle and CUT: the engine returns 1.00x at every
school except Dartmouth, so any "first-gen changes everything" hook would be a
claim our own numbers don't support.

Run daily (launchd com.candor.dailyrigged, ~6:40am) with --n <accounts> or default 8.
--dry to preview without pushing. --angle <name> to force one angle.
"""
import os, sys, json, tempfile, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from factory import (_shot, _title_png, _finish, start_local_server, stop_local_server,
                     LOCAL_URL, CRON_KEY, TARGET_URL)

# No-repeat window. The dedup API keys on SLUG, not (slug, angle), so a school is
# spoken for across every angle once it's used. Two angles give ~30 unique iconic
# slugs; at 8/day a 5-day window needed 40 and so blew through "pool exhausted"
# every 2.5 days and silently started repeating. 3 days needs 24. Raise this only
# alongside more angles or a wider iconic gate.
RECENT_DAYS = 3

# NOT ICONIC ENOUGH TO CARRY A HOT TAKE. These cleared the legacy-multiplier gate
# but a high-schooler scrolling TikTok has no reaction to "Washington and Lee
# admissions are rigged" — the outrage only lands for a school they already have
# feelings about. Hand-excluded 2026-07-26; small LACs, not household names.
NOT_ICONIC = {"bucknell", "oberlin", "swarthmore", "colgate", "wlu", "bowdoin"}

# Beyond the hand-exclusion, gate on REAL demand: a school has to have been run
# through the calculator at least this many times in the last 90 days to be worth
# a hot take. That's our own users voting on which names they care about, which
# beats me guessing at what reads as iconic. Falls open (no gate) if the demand
# API is unreachable, so a network blip degrades to the old behavior instead of
# producing an empty batch.
MIN_DEMAND = 15

# California banned legacy preferences at private colleges (AB 1780, in force
# Sept 2025) and Virginia banned them at publics (2024). A "rigged by legacy" post
# about these is factually wrong now, whatever the historical multiplier says.
# The round and major angles are unaffected and may still use them.
LEGACY_BANNED = {"stanford", "usc", "santa-clara", "pepperdine", "caltech",
                 "uva", "william-mary", "wm", "vt", "gmu"}

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


_demand_cache = None


def _demand():
    """Per-school calc volume (90d) from the live API — our users voting on which
    names they actually care about. {} on any failure, which disables the gate."""
    global _demand_cache
    if _demand_cache is None:
        try:
            url = f"{TARGET_URL}/content/school-demand?key={urllib.parse.quote(CRON_KEY)}"
            with urllib.request.urlopen(url, timeout=15) as r:
                _demand_cache = json.load(r) or {}
        except Exception as e:
            print(f"  demand API unreachable ({e}) — iconic gate disabled")
            _demand_cache = {}
    return _demand_cache


def _iconic(slug):
    """Would a scrolling high-schooler have a reaction to this school's name?"""
    if slug in NOT_ICONIC:
        return False
    d = _demand()
    return (not d) or d.get(slug, 0) >= MIN_DEMAND


def _legacy_pool():
    """Legacy hot take: mult >= 1.8, not ED-only (their OVERALL odds don't move),
    not somewhere legacy is now illegal, iconic, in COLLEGES."""
    return [slug for slug, mult in sorted(app._LEGACY_MULT.items(), key=lambda kv: -kv[1])
            if mult >= 1.8 and slug not in app._LEGACY_ED_ONLY
            and slug not in LEGACY_BANNED and slug in app.COLLEGES_BY_SLUG
            and _iconic(slug)]


def _round_mult(slug):
    """(round_code, multiplier) for the biggest early-vs-RD gap this school offers
    our demo applicant, or None. Uses the same deterministic splitter the product
    uses, so the number on the slide is the number a real user would be shown."""
    sch = app.COLLEGES_BY_SLUG.get(slug)
    detail = app.ADMISSIONS_DETAIL.get(slug) or {}
    if not sch or not detail.get("rounds"):
        return None
    p = _profile(sch["name"])
    p["legacy_schools"] = ""          # legacy would double-count into the Early round
    o = app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    if not o:
        return None
    split = app.deterministic_round_odds(sch, detail, _mid(o) / 100.0)
    if not split or not split.get("RD"):
        return None
    rd = split["RD"]
    best = max(((rc, v) for rc, v in split.items() if rc != "RD"),
               key=lambda kv: kv[1], default=None)
    if not best or rd <= 0:
        return None
    return best[0], best[1] / rd


def _round_pool():
    """Schools with a big enough early-round gap to be worth a take."""
    out = []
    for slug in app.COLLEGES_BY_SLUG:
        if not _iconic(slug):
            continue
        rm = _round_mult(slug)
        if rm and rm[1] >= 1.6:
            out.append((slug, rm[1]))
    return [s for s, _ in sorted(out, key=lambda kv: -kv[1])]


# Majors the engine prices very differently at schools that admit by college
# rather than university-wide. Paired hardest-vs-softest so the gap is honest.
_MAJOR_HARD, _MAJOR_SOFT = "Computer Science", "Classics"


def _major_mult(slug):
    """How much the SAME applicant's odds move on intended major alone."""
    sch = app.COLLEGES_BY_SLUG.get(slug)
    if not sch:
        return None
    ph, ps = _profile(sch["name"]), _profile(sch["name"])
    ph["legacy_schools"] = ps["legacy_schools"] = ""
    ph["major"], ps["major"] = _MAJOR_HARD, _MAJOR_SOFT
    try:
        oh = app.estimate_odds_v2(sch, ph) or app.estimate_odds(sch, app.compute_my_fit(ph, sch), ph)
        os_ = app.estimate_odds_v2(sch, ps) or app.estimate_odds(sch, app.compute_my_fit(ps, sch), ps)
    except Exception:
        return None
    if not oh or not os_ or _mid(os_) <= 0:
        return None
    return _mid(oh) / _mid(os_)


def _major_pool():
    out = []
    for slug in app.COLLEGES_BY_SLUG:
        if not _iconic(slug):
            continue
        m = _major_mult(slug)
        if m and m >= 1.3:
            out.append((slug, m))
    return [s for s, _ in sorted(out, key=lambda kv: -kv[1])]


# Each angle: pool builder + the multiplier it reveals + the two title slides.
# ANGLES is ordered; the daily batch rotates through it so one morning's 8 posts
# don't all make the same argument.
#
# "major" IS IMPLEMENTED BUT NOT IN ROTATION — needs Jasper's sign-off on an odds
# question first. Its whole effect comes from sub_school_for_major(): CS maps to
# the engineering school while Classics falls through to university-wide, so the
# multiplier is really "engineering sub-school vs the university". The engine has
# engineering as EASIER, which is defensible at NYU (Tandon does admit above the
# NYU rate) but backwards at Northeastern (Khoury), Northwestern (McCormick) and
# JHU (Whiting), where CS is the harder admit. Posting "1.8X the odds on major
# alone" for those would be a false claim in public. Fix the engine first, then
# add "major" back to this tuple.
ANGLES = ("round", "legacy")
ALL_ANGLES = ("round", "legacy", "major")


def _pool(angle="legacy"):
    return {"legacy": _legacy_pool, "round": _round_pool, "major": _major_pool}[angle]()


def _recent_rigged_slugs():
    """Slugs used by any rigged/hot-take carousel recently (via live queue API)."""
    try:
        url = f"{TARGET_URL}/content/queue/recent-rigged?key={CRON_KEY}&days={RECENT_DAYS}"
        with urllib.request.urlopen(url, timeout=15) as r:
            return set(json.load(r).get("slugs", []))
    except Exception:
        return set()


def _mtxt(mult):
    """2.0 -> '2X', 2.7 -> '2.7X'. Round numbers read harder on a title slide."""
    return f"{int(round(mult))}X" if abs(mult - round(mult)) < 0.15 else f"{round(mult,1)}X"


# Per-angle copy. Each returns (profile, multiplier, title_lines_1, title_lines_2,
# highlight_words) or None to skip. The applicant on the profile slide has to be
# the one the multiplier was computed for, or the reveal is a lie — that's why
# each angle owns its own profile rather than sharing one.
def _angle_legacy(slug, sch, name, short):
    p = _profile(name)
    p_no = dict(p); p_no["legacy_schools"] = ""
    o0 = app.estimate_odds_v2(sch, p_no) or app.estimate_odds(sch, app.compute_my_fit(p_no, sch), p_no)
    o1 = app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    mult = _mid(o1) / max(0.1, _mid(o0))
    if mult < 1.6:
        return None
    m = _mtxt(mult)
    return (p, o1, m,
            [short.upper() + " ADMISSIONS", "ARE RIGGED"],
            ["THIS APPLICANT HAD", f"{m} HIGHER ODDS", "BECAUSE OF LEGACY"],
            ["RIGGED"], [m, "LEGACY"])


def _angle_round(slug, sch, name, short):
    rm = _round_mult(slug)
    if not rm or rm[1] < 1.6:
        return None
    rc, mult = rm
    m = _mtxt(mult)
    p = _profile(name); p["legacy_schools"] = ""
    o = app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    label = {"ED": "EARLY DECISION", "ED1": "EARLY DECISION", "ED2": "ED II",
             "REA": "RESTRICTIVE EARLY", "EA": "EARLY ACTION"}.get(rc, "APPLYING EARLY")
    return (p, o, m,
            ["APPLYING TO " + short.upper(), "REGULAR DECISION", "IS THE MISTAKE"],
            ["SAME APPLICANT.", f"{m} THE ODDS", f"IN {label}"],
            ["MISTAKE"], [m, label])


def _angle_major(slug, sch, name, short):
    mult = _major_mult(slug)
    if not mult or mult < 1.3:
        return None
    m = _mtxt(mult)
    p = _profile(name); p["legacy_schools"] = ""; p["major"] = _MAJOR_HARD
    o = app.estimate_odds_v2(sch, p) or app.estimate_odds(sch, app.compute_my_fit(p, sch), p)
    return (p, o, m,
            [short.upper() + " DOESN'T", "ADMIT STUDENTS.", "IT ADMITS MAJORS"],
            ["SAME GRADES.", "SAME SCORES.", f"{m} THE ODDS", "ON MAJOR ALONE"],
            ["MAJORS"], [m, "MAJOR ALONE"])


_ANGLE_FN = {"legacy": _angle_legacy, "round": _angle_round, "major": _angle_major}


def build(slug, angle="legacy", dry=False):
    sch = app.COLLEGES_BY_SLUG[slug]
    name = sch["name"]
    short, accent = app._school_brand(slug, name)
    got = _ANGLE_FN[angle](slug, sch, name, short)
    if not got:   # reveal wouldn't look dramatic — skip rather than post a dud
        print(f"  skip {slug} [{angle}]: multiplier too small to carry the take")
        return None
    p, o1, m, t1, t2, hi1, hi2 = got

    app.save_profile(181, p)
    img1 = _title_png(slug, t1, hi1)
    img2 = _title_png(slug, t2, hi2)
    tmp = tempfile.mkdtemp(prefix="drig_")
    prof = _shot(f"{tmp}/p.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181")
    chances = _shot(f"{tmp}/c.png", f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1", static=True)

    payload = dict(
        school_slug=slug, school_name=name, accent=accent,
        title_text=" ".join(t1), title_formula="daily-rigged", slide3_type="chances",
        profile_json=json.dumps(p), odds_text=f"{o1[0]}-{o1[1]}%", grade_text="",
        priority=50,
        img1=img1, img2=img2, img3=prof, img4=chances,
        meta={"hot_take": f"{angle}_daily", "angle": angle, "mult": m, "lines": t1},
    )
    return _finish(payload, dry, f"DAILY {angle.upper()} {slug} ({m})")


def main():
    dry = "--dry" in sys.argv
    n = 8
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    only = None
    if "--angle" in sys.argv:
        only = sys.argv[sys.argv.index("--angle") + 1]
        if only not in ALL_ANGLES:
            sys.exit(f"--angle must be one of {', '.join(ALL_ANGLES)}")
    angles = (only,) if only else ANGLES

    used = _recent_rigged_slugs()
    # Build the day's worklist by DEALING angles round-robin, so 8 posts come out
    # as round/legacy/major/round/… instead of eight of the same argument. Each
    # angle keeps its own no-repeat bookkeeping against `used`, and a school
    # already spoken for today can still appear under a different angle — the
    # take is what's repetitive, not the school.
    pools = {a: [s for s in _pool(a) if s not in used] for a in angles}
    for a in angles:
        print(f"  pool[{a}] = {len(pools[a])} schools")
    if not any(pools.values()):
        print("every angle exhausted — everything used recently; widening")
        pools = {a: _pool(a) for a in angles}

    todo, seen = [], set()
    while len(todo) < n + 4 and any(pools.values()):
        for a in angles:
            if not pools[a]:
                continue
            slug = pools[a].pop(0)
            if (slug, a) in seen:
                continue
            seen.add((slug, a))
            todo.append((slug, a))
            if len(todo) >= n + 4:
                break

    made = 0
    start_local_server()
    try:
        for slug, angle in todo:
            if made >= n:
                break
            try:
                if build(slug, angle=angle, dry=dry):
                    made += 1
            except Exception as e:
                print(f"  FAILED {slug} [{angle}]: {e}")
    finally:
        stop_local_server()
    print(f"done: {made}/{n} hot-take carousels" + (" (dry)" if dry else ""))


if __name__ == "__main__":
    main()
