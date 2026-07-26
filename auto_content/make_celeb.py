#!/usr/bin/env python3
"""'Would <celebrity> get into their own college today?' carousels.

The one format where the hook is a FACT, not a simulated applicant: the admit
rate the year they were admitted vs the admit rate now. That comparison is the
whole Candor thesis in two numbers — the door narrowed, and the kids getting
rejected today are not worse than the people already inside.

Hard rule this generator enforces structurally: it never renders a celebrity's
GPA, test scores, or any academic record. There is no profile slide (the staple
formats have one; this one deliberately does not), no saved demo profile, and
the reveal route (/celeb/export) can only read facts from celebs.json — not from
query params. Fabricating an academic record for a real, identifiable person is
a legal problem, so the safest design is one where the code physically can't.

Facts live in auto_content/celebs.json, hand-researched with a source note per
entry. Anything unverifiable for the exact cycle carries the earliest
well-documented rate plus a year_label that says so on the slide.

3 slides: cover hook -> the two numbers -> the sourced then/now reveal.
Run: python3 auto_content/make_celeb.py --n 4 [--dry] [--id emma-watson-brown]
"""
import os, sys, json, tempfile, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from factory import (_shot, _title_png, _finish, start_local_server, stop_local_server,
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

    # Cover asks the question; it never answers it. "Would they get in today?" is
    # a question about the odds, and leaving it open is both the honest framing
    # and the reason anyone swipes.
    t1 = ["WOULD", name, f"GET INTO {short}", "TODAY?"]
    # Slide 2 is the payoff and it is pure arithmetic — no adjectives, nothing
    # about the person. Phrased with "they" so one template covers everyone, and
    # the year sits on its own line so a fuzzy label ("the late 1990s") reads as
    # a sentence instead of a parenthetical.
    t2 = [f"{short} TOOK {_fmt(then)}", f"IN {when.upper()}", f"TODAY: {_fmt(now)}"]
    img1 = _title_png(slug, t1, [name, short])
    # Accent the bare numbers, not "38.9%" — mark_accents anchors on \b, and a
    # trailing '%' is not a word char, so the %-suffixed form never matches and
    # the line renders flat black.
    img2 = _title_png(slug, t2, [_fmt(then).rstrip("%"), _fmt(now).rstrip("%")])
    tmp = tempfile.mkdtemp(prefix="celeb_")
    # static=True: the reveal route runs the bullets LLM, which on the Max-CLI
    # path outlasts Chrome's paint budget and ships a black slide. Fetch once,
    # screenshot the already-rendered HTML (same fix as chances/compare).
    reveal = _shot(f"{tmp}/r.png",
                   f"{LOCAL_URL}/celeb/export?id={c['id']}&rkey={CRON_KEY}&clean=1", static=True)

    payload = dict(
        school_slug=slug, school_name=merged["name"], accent=accent,
        title_text=" ".join(t1), title_formula="celeb", slide3_type="celeb",
        # Empty on purpose: there is no applicant here. Writing a profile blob
        # would be inventing an academic record for a real person.
        profile_json="{}",
        odds_text=f"{_fmt(then)} → {_fmt(now)}", grade_text="",
        img1=img1, img2=img2, img3=reveal,
        meta={"celeb": c["id"], "celeb_name": c["name"], "then": then, "now": now,
              "harder": harder, "year_label": when, "source_note": c.get("source_note", ""),
              "lines": t1},
    )
    res = _finish(payload, dry, f"CELEB {c['name']} / {short} ({_fmt(then)} -> {_fmt(now)}, {harder}x)")
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
