#!/usr/bin/env python3
"""'Would <celebrity> get into their own college today?' carousels.

The hook is a FACT — the admit rate the year they were admitted vs the rate now.
That comparison is the whole Candor thesis in two numbers: the door narrowed, and
the kids getting rejected today are not worse than the people already inside.

But the hook is not the payload. Like every other format here, the carousel has
to end with the viewer watching Candor actually COMPUTE something, so slides 3
and 4 are the real product screens (/profile/<slug>/export and
/chances/<slug>/export) — the same two the daily-rigged generator screenshots.

Squaring that with the accuracy rule: what goes through the engine is an
ARCHETYPE, not the celebrity. We do not know anyone's GPA or test scores, so we
don't claim any. The archetype is a strong applicant of the kind that cleared
the bar in that era, both product slides carry an on-slide "hypothetical, not
their real stats" stamp (?note=), and the only person-specific input is their
publicly documented field of study — a fact, not an academic record. The
factual claims (school, year, admit rate then vs now) stay on slides 1-2 where
they're sourced; the engine output on slides 3-4 is honestly framed as "here's
what Candor says about a profile like this applying there today."

Facts live in auto_content/celebs.json, hand-researched with a source note per
entry. Anything unverifiable for the exact cycle carries the earliest
well-documented rate plus a year_label that says so on the slide.

4 slides: "CAN <PERSON> GET INTO <SCHOOL>?" over a photo+logo band -> the
sourced then/now -> the archetype profile card -> Candor's real odds for it
today. Cover photos are optional files in static/celebs/<id>.jpg; a missing one
degrades to a logo-only cover (see that folder's README).
Run: python3 auto_content/make_celeb.py --n 4 [--dry] [--id emma-watson-brown]
"""
import os, sys, json, tempfile, datetime, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from factory import (_shot, _finish, start_local_server, stop_local_server,
                     LOCAL_URL, CRON_KEY)

HERE = os.path.dirname(os.path.abspath(__file__))
# Don't reuse a celebrity for this many days. The pool is ~30 and the payoff is
# the surprise of the pairing, so a repeat inside a couple of weeks burns it.
RECENT_DAYS = 21
_STATE = os.path.join(HERE, ".celeb_state.json")
# Below this the reveal is a shrug, not a hook — a 1.2x change reads as noise to
# a viewer and makes Candor look like it's manufacturing outrage. Same instinct
# as make_daily_rigged's 1.6x floor on the legacy multiplier.
MIN_HARDER = float(os.environ.get("CELEB_MIN_HARDER", "1.6"))


def _state():
    try:
        return json.load(open(_STATE))
    except Exception:
        return {}


def _recent_ids():
    """Celebrity ids used in the last RECENT_DAYS days. Kept in a local file
    rather than the live queue API (unlike make_daily_rigged) because the queue
    has no per-celebrity field to query — the id only exists here."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS)).isoformat()
    return {cid for cid, day in _state().items() if day >= cutoff}


def _mark_used(cid):
    d = _state()
    d[cid] = datetime.date.today().isoformat()
    # Prune anything older than 4x the window so the file can't grow forever.
    old = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS * 4)).isoformat()
    d = {k: v for k, v in d.items() if v >= old}
    try:
        json.dump(d, open(_STATE, "w"))
    except Exception:
        pass


def _fmt(pct):
    """13.8 -> '13.8%', 5.0 -> '5%'. Trailing '.0' on a slide reads like a typo."""
    return (f"{pct:.1f}".rstrip("0").rstrip(".")) + "%"


# The archetype fed to the engine on slides 3-4. Deliberately GENERIC and
# deliberately strong: a well-rounded, no-national-hook applicant — the profile
# that comfortably cleared a 15-20% admit rate and gets shredded by a 4% one.
# That contrast IS the format's argument, and it's an argument about the bar
# moving, not about any real person's record.
#
# is_exceptional=0 and a mid ec_rating on purpose: hand it a national-tier hook
# and today's odds stop collapsing, which would kill the payoff AND overstate
# how forgiving admissions still is.
def _archetype(major):
    return dict(
        uw_gpa=3.92, weighted_gpa=4.35, sat=1500, sat_math=760, sat_ebrw=740,
        major=major or "Economics", state="Connecticut", school_type="private",
        aps="AP English Literature, AP US History, AP Calculus AB, AP Biology, AP Psychology",
        ecs="Varsity Athlete (3 years)\nSchool Newspaper\nDebate Team\nVolunteer Tutor",
        leadership="Newspaper Editor, Debate Team Captain",
        awards="AP Scholar with Distinction, National Honor Society",
        legacy_schools="", first_gen=0, athlete=0, is_exceptional=0, ec_rating=380,
    )


def build(c, dry=False):
    slug = c["slug"]
    sch = app.COLLEGES_BY_SLUG[slug]
    merged = app.merged_school(sch)
    then = float(c["admit_rate_then"])
    now = round(merged["accept"] * 100, 1)
    harder = app._celeb_harder(then, now)
    if not harder or harder < MIN_HARDER:
        print(f"  skip {c['id']}: only {harder}x harder ({then}% -> {now}%)")
        return None
    short, accent = app._school_brand(slug, merged["name"])
    when = c.get("year_label") or str(c.get("entry_year", ""))
    name = c["name"].upper()

    # Cover asks the question, never answers it — "CAN X GET INTO Y?" asserts
    # nothing about anyone's record, which is exactly why it's the safe framing
    # AND the reason anyone swipes. Built server-side by /title-celeb/export so
    # the name and school come from the dataset, not from a URL we assembled.
    t1 = ["CAN", name, "GET INTO", f"{short}?"]
    tmp = tempfile.mkdtemp(prefix="celeb_")
    photo = app.celeb_photo_url(c["id"])
    if not photo:
        # Degrade to a logo-only band rather than a broken image box. Worth
        # printing: the cover is much stronger with the face, so a run that logs
        # a pile of these is a prompt to go fill in static/celebs/.
        print(f"  note: no photo for {c['id']} — cover falls back to logo only")
    img1 = _shot(f"{tmp}/t.png", f"{LOCAL_URL}/title-celeb/export?id={c['id']}&rkey={CRON_KEY}&clean=1")

    # Slide 2 — the sourced fact. The old plain-text "X TOOK 38.9% / TODAY 5.4%"
    # title slide was cut for this: /celeb/export carries the same two numbers
    # AND the source line, and the queue only holds four images, so the product
    # slides get the last two slots.
    reveal = _shot(f"{tmp}/r.png",
                   f"{LOCAL_URL}/celeb/export?id={c['id']}&rkey={CRON_KEY}&clean=1", static=True)

    # Slides 3-4 — the actual product. Save the archetype to the demo user, then
    # screenshot the same two routes the daily-rigged generator uses, so the
    # viewer ends the carousel having watched Candor read a profile and print
    # odds, not having read a trivia card. c["major"] is their real, publicly
    # documented field of study — a fact, so it's safe to feed the engine.
    # Nothing else in that profile is theirs, which is what the stamp says.
    p = _archetype(c.get("major"))
    app.save_profile(181, p)
    # Same compute_fit -> estimate_odds path /chances/<slug>/export uses, so the
    # odds_text we file in the queue can't disagree with the number printed on
    # the slide (estimate_odds_v2 reads a couple of points lower here).
    _fit = app.compute_fit(p, merged)[0]
    odds = app.estimate_odds(merged, _fit, p)
    # The stamp names the person on purpose: "hypothetical profile" alone could
    # still be read as somebody's estimate OF them.
    stamp = urllib.parse.quote(f"Hypothetical profile — not {c['name']}'s real stats")
    prof = _shot(f"{tmp}/p.png",
                 f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181&note={stamp}")
    chances = _shot(f"{tmp}/c.png",
                    f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1&note={stamp}",
                    static=True)

    payload = dict(
        school_slug=slug, school_name=merged["name"], accent=accent,
        title_text=" ".join(t1), title_formula="celeb",
        # 'chances' so the release gate and the TikTok perf feedback bucket this
        # with the other odds-reveal carousels — the payoff slide IS a chances
        # slide. meta.celeb is what identifies the format.
        slide3_type="chances",
        profile_json=json.dumps(p),
        odds_text=f"{odds[0]}-{odds[1]}%", grade_text="",
        img1=img1, img2=reveal, img3=prof, img4=chances,
        meta={"celeb": c["id"], "celeb_name": c["name"], "then": then, "now": now,
              "harder": harder, "year_label": when, "source_note": c.get("source_note", ""),
              "archetype": True, "lines": t1},
    )
    res = _finish(payload, dry,
                  f"CELEB {c['name']} / {short} ({_fmt(then)} -> {_fmt(now)}, {harder}x) "
                  f"| today {odds[0]}-{odds[1]}%")
    if not dry:
        _mark_used(c["id"])
    return res


def _pool():
    """Celebrities whose school we can actually render, freshest first: anything
    not used inside RECENT_DAYS, ordered by how dramatic the drop is (biggest
    delta = strongest hook), so a short run posts the best ones."""
    used = _recent_ids()
    out = []
    for c in app.celeb_entries():
        if c.get("id") in used:
            continue
        merged = app.merged_school(app.COLLEGES_BY_SLUG[c["slug"]])
        h = app._celeb_harder(float(c["admit_rate_then"]), round(merged["accept"] * 100, 1))
        if h and h >= MIN_HARDER:
            out.append((h, c))
    out.sort(key=lambda hc: -hc[0])
    return [c for _h, c in out]


def main():
    dry = "--dry" in sys.argv
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 4
    if "--id" in sys.argv:
        one = app.celeb_by_id(sys.argv[sys.argv.index("--id") + 1])
        todo = [one] if one else []
        if not todo:
            print("no such celeb id — see auto_content/celebs.json"); sys.exit(1)
        n = 1
    else:
        # Generous spares: build() skips thin deltas and the loop below skips
        # repeat schools, so a tight slice can starve a run of 4.
        todo = _pool()[: n * 3 + 4]
        if not todo:
            print("pool exhausted — every celeb used recently; widening")
            todo = app.celeb_entries()[: n * 3 + 4]
    made = 0
    seen_slugs = set()
    start_local_server()
    try:
        for c in todo:
            if made >= n:
                break
            # The pool is sorted by biggest drop, and the biggest drops cluster at
            # one school (Penn's 38.9% -> 5.4% eras). Without this, a batch of 4
            # ships four Penn carousels to four different accounts on the same day.
            if c["slug"] in seen_slugs and "--id" not in sys.argv:
                continue
            try:
                if build(c, dry=dry):
                    made += 1
                    seen_slugs.add(c["slug"])
            except Exception as e:
                print(f"  FAILED {c.get('id')}: {e}")
    finally:
        stop_local_server()
    print(f"done: {made}/{n} celeb carousels" + (" (dry)" if dry else ""))


if __name__ == "__main__":
    main()
