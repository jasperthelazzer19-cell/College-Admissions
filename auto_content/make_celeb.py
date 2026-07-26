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

────────────────────────────────────────────────────────────────────────────
SECOND TRACK: --track hypo  (roster: auto_content/celebs_hypothetical.json)

Same cover question, different argument. These people did NOT attend the school
on the slide, so there is no then/now admit rate to anchor on. What anchors it
is the payoff question the whole feed is really about: does being the best on
Earth at one thing beat grades?

The accuracy line is the SAME line, held from the other side. On the factual
track nothing person-specific reaches the engine except their documented major.
Here the person-specific input is their REAL, publicly documented achievement
and awards — also fact — and it is the only thing about them the engine sees.
The GPA and SAT are invented, and they are labelled as invented ON THE SLIDE in
the roster entry's own academic_note wording (slide 2's stamp box, and the
?note= band on slides 3-4). The title stays a question. We are not claiming
anyone's transcript; we are asking what Candor does with a résumé like that.

Why this only works after 2026-07-26: _v2_spike_odds_ratio. Before that fix an
elite spike didn't move the number and every entry returned a flat 1-2%, which
is not a carousel. See _hypo_profile for why the spike has to be set explicitly
rather than read off the achievement text.
Run: python3 auto_content/make_celeb.py --track hypo --n 4 [--dry] [--id ...]
"""
import os, sys, json, tempfile, datetime, urllib.parse

# The hypo track must cost $0, and the strip has to happen BEFORE `import app`
# because app builds its Anthropic client at import time. Gated on the flag
# rather than unconditional: the factual track's slide 2 (/celeb/export) does
# generate bullets, and yanking the key out from under it would silently change
# an existing, working format. Same guard scripts/smoke.py and odds_regress use.
if "hypo" in sys.argv:
    for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"):
        os.environ.pop(_k, None)

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


# Both tracks keep their own state file (path passed in). Sharing one would make
# a factual Emma Watson carousel block a hypothetical entry that has nothing to
# do with it — the ids don't even collide, but the rosters are different pools
# with different sizes and the no-repeat window should be measured per pool.
def _state(path=_STATE):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _recent_ids(path=_STATE):
    """Celebrity ids used in the last RECENT_DAYS days. Kept in a local file
    rather than the live queue API (unlike make_daily_rigged) because the queue
    has no per-celebrity field to query — the id only exists here."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS)).isoformat()
    return {cid for cid, day in _state(path).items() if day >= cutoff}


def _mark_used(cid, path=_STATE):
    d = _state(path)
    d[cid] = datetime.date.today().isoformat()
    # Prune anything older than 4x the window so the file can't grow forever.
    old = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS * 4)).isoformat()
    d = {k: v for k, v in d.items() if v >= old}
    try:
        json.dump(d, open(path, "w"))
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


# ── hypothetical track ─────────────────────────────────────────────────────
_HYPO_STATE = os.path.join(HERE, ".celeb_hypo_state.json")
# Categories that rot. The worldcup block was researched a week after the 2026
# final and is the most perishable content in either roster, so the default pool
# floats it to the front instead of leaving it to a random draw — by the time a
# 30-entry pool cycles round to it, "one week after the final" is gone.
PERISHABLE = ("worldcup",)

# The EC rating the engine is told these profiles carry, and the explicit
# exceptional flag.
#
# WHY THIS IS SET AND NOT MEASURED, which is the one judgement call in this file:
# ec_rating_keyword() is a high-school-activity-list heuristic. Run it over this
# roster and every single entry scores 60-200 out of 1000 — "Most decorated
# gymnast in history" and "All-time leading goalscorer in World Cup history"
# contain none of the tokens it looks for (ISEF, USAMO, Intel, national merit).
# The keyword rater isn't wrong about high schoolers; it simply has no vocabulary
# for an adult world-class career, and letting it grade these would report
# strength 0.00 for all 30, flat 1-2% odds, and a dead format.
#
# So the flag encodes the roster's own selection criterion — every entry is here
# BECAUSE they are top-of-the-planet at one thing, which is exactly what
# is_exceptional means (top 1-2% nationally; these clear it by orders of
# magnitude). That is a judgement about a REAL, documented achievement, not an
# invented credential, so it stays on the fact side of this format's line.
#
# Consequence to be honest about in any report: the spike therefore fires for
# every entry by construction. The thing that actually varies between entries is
# the hypothetical GPA/SAT and the school, which is where the interesting spread
# in the odds comes from.
HYPO_EC_RATING = 950
def _hypo_profile(c):
    """The profile fed to the engine. Person-specific fields are the roster's
    REAL achievement/awards text and their intended major; everything else is
    either invented-and-labelled (gpa/sat) or neutral scaffolding.

    achievement goes into `ecs` whole — every line, no truncation. Rodri's real
    Jaume I business degree lives in that list and is the strongest fact in the
    file, so nothing here is allowed to trim it. The profile export renders each
    newline as its own bullet and scales the card to fit rather than clipping."""
    gpa = float(c["gpa"])
    return dict(
        uw_gpa=gpa, weighted_gpa=round(gpa + 0.35, 2),
        sat=int(c["sat"]), sat_math=None, sat_ebrw=None,
        major=c.get("major") or "Undecided",
        # state="" ON PURPOSE. The profile export renders a "<State> Resident"
        # bullet whenever this is set, which for Rodri came out as "California
        # Resident" — an invented biographical claim about a real Spaniard,
        # sitting one bullet above his real degree where it reads as equally
        # sourced. Blank drops the line entirely. Side effect is correct anyway:
        # the public entries (UCLA, Berkeley, Michigan) now price as out-of-state,
        # which is the honest default for people who mostly aren't American.
        state="", school_type="public",
        # No AP list on purpose. Inventing a course load is a second fabricated
        # academic claim, and the format's whole point is "ordinary academics" —
        # the note already tells the viewer the two numbers are made up, and one
        # made-up thing is easier to label honestly than three.
        aps="",
        ecs=c.get("achievement") or "",
        leadership="",
        awards=c.get("awards") or "",
        legacy_schools="", first_gen=0, athlete=0,
        is_exceptional=1, ec_rating=HYPO_EC_RATING,
    )


def _persist_spike(uid, p):
    """Write ec_rating / is_exceptional straight onto the demo profile row.

    save_profile does NOT persist either one — it re-derives ec_rating from the
    grader (or the keyword rater) and never touches is_exceptional. So without
    this the generator would compute odds from a spiked dict while
    /chances/<slug>/export re-read an unspiked row and rendered a totally
    different number onto the actual slide. Setting exceptional_evaluated_at to
    now also parks get_or_evaluate_exceptionality on the cached verdict, which
    is what keeps this whole track free of LLM calls."""
    with app.db() as conn:
        conn.execute(
            "UPDATE profiles SET ec_rating=?, is_exceptional=1, exceptional_reason=?, "
            "exceptional_evaluated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (p.get("ec_rating"), "world-class public achievement (celeb hypothetical track)", uid))
        conn.commit()


def _spike_strength(p):
    """The 0-1 signal _v2_spike_odds_ratio keys off. Printed per entry so a run
    log proves the spike fired rather than assuming it did."""
    try:
        return float(app.ec_exceptional_strength(p) or 0.0)
    except Exception:
        return 0.0


def build_hypo(c, dry=False):
    """4 slides: the question -> the setup (real résumé + labelled fake grades)
    -> Candor's real profile card -> Candor's real odds."""
    slug = c["slug"]
    sch = app.COLLEGES_BY_SLUG[slug]
    merged = app.merged_school(sch)
    short, accent = app._school_brand(slug, merged["name"])
    name = c["name"].upper()

    p = _hypo_profile(c)
    strength = _spike_strength(p)
    if strength <= 0:
        # Loud, not silent. A zero here means the odds slide would read a flat
        # 1-2% and the carousel would answer its own question with "no, and also
        # nothing you did mattered" — which is not the argument and not true.
        print(f"  SKIP {c['id']}: spike signal is 0.00 — odds would be flat, carousel is pointless")
        return None

    app.save_profile(181, p)
    _persist_spike(181, p)
    # Read the odds back through the SAME path /chances/<slug>/export uses
    # (_chances_profile -> compute_fit -> estimate_odds) rather than from the
    # dict, so the number filed in the queue cannot disagree with the number
    # printed on slide 4. The dict and the row differ by construction here —
    # save_profile rewrites ec_rating — so this is not paranoia.
    row = app._chances_profile(181, slug)
    fit = app.compute_fit(row, merged)[0]
    odds = app.estimate_odds(merged, fit, row)

    tmp = tempfile.mkdtemp(prefix="celebhypo_")
    if not app.celeb_photo_url(c["id"]):
        print(f"  note: no photo for {c['id']} — cover falls back to logo only")
    img1 = _shot(f"{tmp}/t.png",
                 f"{LOCAL_URL}/title-celeb/export?id={c['id']}&track=hypo&rkey={CRON_KEY}&clean=1")
    # Slide 2 is static server-rendered HTML with no LLM in it, so it needs no
    # warm-fetch — but static=True still costs nothing and keeps the render path
    # identical to every other reveal slide.
    setup = _shot(f"{tmp}/s.png",
                  f"{LOCAL_URL}/celeb-hypo/export?id={c['id']}&rkey={CRON_KEY}&clean=1", static=True)

    # The stamp is the entry's OWN academic_note, verbatim. Not a template built
    # from gpa/sat: Rodri's note is deliberately worded differently (his degree is
    # real, only the test numbers are invented) and a generic "hypothetical
    # profile" stamp would flatten exactly the distinction that makes his the
    # strongest entry in the file.
    stamp = urllib.parse.quote(c.get("academic_note") or
                               f"Hypothetical academics — not {c['name']}'s real stats")
    prof = _shot(f"{tmp}/p.png",
                 f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181&note={stamp}")
    chances = _shot(f"{tmp}/c.png",
                    f"{LOCAL_URL}/chances/{slug}/export?uid=181&rkey={CRON_KEY}&clean=1&note={stamp}",
                    static=True)

    t1 = ["CAN", name, "GET INTO", f"{short}?"]
    payload = dict(
        school_slug=slug, school_name=merged["name"], accent=accent,
        title_text=" ".join(t1), title_formula="celeb-hypo",
        slide3_type="chances",
        profile_json=json.dumps(p),
        odds_text=f"{odds[0]}-{odds[1]}%", grade_text="",
        img1=img1, img2=setup, img3=prof, img4=chances,
        meta={"celeb": c["id"], "celeb_name": c["name"], "celeb_track": "hypo",
              "category": c.get("category", ""), "spike_strength": round(strength, 3),
              "hypothetical_gpa": c.get("gpa"), "hypothetical_sat": c.get("sat"),
              "academic_note": c.get("academic_note", ""), "lines": t1},
    )
    res = _finish(payload, dry,
                  f"CELEB-HYPO {c['name']} / {short} (spike {strength:.2f}) "
                  f"| {odds[0]}-{odds[1]}%")
    if not dry:
        _mark_used(c["id"], _HYPO_STATE)
    return res


def _hypo_pool(category=None):
    """Unused-recently entries, perishable categories first.

    No quality gate like the factual track's MIN_HARDER — every entry here is
    renderable by construction. The only ordering opinion is freshness: a
    worldcup entry is worth strictly more today than it will be in a month."""
    used = _recent_ids(_HYPO_STATE)
    out = [c for c in app.celeb_entries("hypo") if c.get("id") not in used]
    if category:
        out = [c for c in out if (c.get("category") or "").lower() == category.lower()]
    # stable sort: perishable block keeps its authored order, then everything else
    out.sort(key=lambda c: PERISHABLE.index(c.get("category"))
             if c.get("category") in PERISHABLE else len(PERISHABLE))
    return out


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


def _arg(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main():
    dry = "--dry" in sys.argv
    n = int(_arg("--n")) if "--n" in sys.argv else 4
    track = (_arg("--track") or "factual").lower()
    if track not in ("factual", "hypo"):
        sys.exit("--track must be 'factual' or 'hypo'")
    category = _arg("--category")
    if category and track != "hypo":
        sys.exit("--category only applies to --track hypo (celebs.json has no categories)")
    if track == "hypo":
        return _main_hypo(dry, n, category)
    if "--id" in sys.argv:
        one = app.celeb_by_id(_arg("--id"))
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


def _main_hypo(dry, n, category):
    if "--id" in sys.argv:
        one = app.celeb_by_id(_arg("--id"), "hypo")
        if not one:
            print("no such id — see auto_content/celebs_hypothetical.json"); sys.exit(1)
        todo, n = [one], 1
    else:
        todo = _hypo_pool(category)
        if not todo and category:
            print(f"no unused entries in category '{category}'"); sys.exit(1)
        if not todo:
            print("pool exhausted — every entry used recently; widening")
            todo = app.celeb_entries("hypo")
        # Spares: unlike the factual track nothing gets skipped for a thin
        # multiplier, but a render can still fail, so keep a couple in hand.
        todo = todo[: n + 3]
    made = 0
    seen_slugs = set()
    start_local_server()
    try:
        for c in todo:
            if made >= n:
                break
            # Two Harvard entries sit adjacent in the worldcup block (Yamal and
            # Mbappé). Without this a --n 4 burst ships two Harvard carousels to
            # two accounts the same morning. --id and an explicit --category
            # burst opt out: if you asked for the worldcup 7, you want all 7.
            if c["slug"] in seen_slugs and not category and "--id" not in sys.argv:
                continue
            try:
                if build_hypo(c, dry=dry):
                    made += 1
                    seen_slugs.add(c["slug"])
            except Exception as e:
                print(f"  FAILED {c.get('id')}: {e}")
    finally:
        stop_local_server()
    print(f"done: {made}/{n} hypothetical celeb carousels" + (" (dry)" if dry else ""))


if __name__ == "__main__":
    main()
