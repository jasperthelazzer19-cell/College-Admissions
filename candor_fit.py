"""Candor "Where should they go?" content helper — driven by Candor's REAL My Fit
engine (app.compute_my_fit / school_match), not a parallel copy.

Flow: pick a vibe-rich seed school -> read its REAL attributes via app's own
helpers (climate_of / sports_strength / greek_strength / setting_of / size bucket)
-> set those as a student's preferences -> let app.compute_my_fit rank EVERY school
-> the #1 is Candor's honest My Fit pick (the reveal). The trait slide shows the
prefs the student set. So the carousel is literally the product's output.

`A` in every signature is the app module (passed in to avoid an import cycle;
app does not import this file).
"""
import random

SIZE_ORDER = ["xs", "small", "medium", "ml", "large"]

# Reach/"dream school" names for the Dream-vs-Best-Fit variant.
DREAM_NAMES = ["harvard", "stanford", "mit", "yale", "princeton", "columbia", "upenn",
               "brown", "duke", "uchicago", "cornell", "dartmouth", "ucla", "ucb", "nyu"]


def base_profile(major=None, state="California"):
    """A complete, strong synthetic applicant so compute_my_fit's admit-realism +
    academic components run without an LLM call (keeps best-fit $0 + deterministic).
    Fit is 80% preferences, so the exact academics barely move the ranking."""
    return {
        "uw_gpa": 3.92, "weighted_gpa": 4.4, "sat": 1490, "act": 33,
        "major": major or "Business", "state": state, "school_type": "public",
        "ecs": "Founded a club; varsity sport; part-time job",
        "leadership": "Team captain; club president",
        "awards": "AP Scholar with Distinction", "aps": "AP Calculus BC, AP Economics, AP English",
    }


def vibe_pool(slugs, A):
    """Seed schools that make a GOOD best-fit debate: real sports-strong or
    Greek-strong schools (per app's own data). Falls back to all if thin."""
    pool = [s for s in slugs
            if A.sports_strength(A.COLLEGES_BY_SLUG[s]) == "strong"
            or A.greek_strength(A.COLLEGES_BY_SLUG[s]) == "strong"]
    return pool or list(slugs)


def prefs_for_school(merged_school, A):
    """Build pref_* values that match a seed school's REAL attributes, using app's
    own helpers — so the resulting My Fit ranking points at that kind of school."""
    climate = A.climate_of(merged_school)            # warm / mild / cold
    sports = A.sports_strength(merged_school)         # strong / medium / low
    greek = A.greek_strength(merged_school)           # strong / medium / light
    setting = A.setting_of(merged_school)             # urban / college_town / suburban / rural
    size_b = A._bucket_of(merged_school.get("size", 0) or 0, A.SIZE_RANGES, SIZE_ORDER)
    prefs = {"pref_weather": climate, "pref_setting": setting, "pref_size": size_b}
    if sports == "strong":
        prefs["pref_sports"] = "strong"
    elif sports == "low":
        prefs["pref_sports"] = "low"
    if greek == "strong":
        prefs["pref_greek"] = "strong"
    elif greek == "light":
        prefs["pref_greek"] = "avoid"
    majors = merged_school.get("majors") or []
    if majors:
        prefs["major"] = majors[0]
        prefs["pref_major_strength"] = "top"
    return prefs


def rank_by_fit(profile, slugs, A):
    """Run the REAL engine over every renderable school -> sorted [(slug, score)]."""
    scored = []
    for s in slugs:
        try:
            merged = A.merged_school(A.COLLEGES_BY_SLUG[s])
            score, _ = A.compute_my_fit(profile, merged)
            scored.append((s, score))
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    return scored


_TRAIT = {
    "weather": {"warm": "Warm, sunny weather", "cold": "Cold winters and real seasons",
                "mild": "Mild, easy weather"},
    "sports": {"strong": "Big-time college sports", "low": "Low-key on sports"},
    "greek": {"strong": "A big Greek life scene", "avoid": "No Greek life"},
    "size": {"xs": "A small, tight-knit campus", "small": "A smaller campus",
             "medium": "A mid-sized campus", "ml": "A big campus", "large": "A huge campus"},
    "setting": {"urban": "A big-city campus", "college_town": "A classic college town",
                "suburban": "A suburban campus", "rural": "A rural campus"},
}


def traits_from_prefs(prefs, n=5):
    """Human-readable lifestyle traits = the prefs the student set (what My Fit ran)."""
    out = []
    for key, pref_key in (("weather", "pref_weather"), ("sports", "pref_sports"),
                          ("greek", "pref_greek"), ("size", "pref_size"),
                          ("setting", "pref_setting")):
        v = prefs.get(pref_key)
        if v and v in _TRAIT[key]:
            out.append(_TRAIT[key][v])
    if prefs.get("major"):
        out.append(f"Strong {prefs['major']}")
    return out[:n]


# Each pref value maps to a LIST of equivalent concrete phrasings — same meaning,
# different words. title_wants picks one at random so two carousels with the same
# seed school don't read identically. Keep these specific and student-real (the
# things people actually weigh when picking a school), not vague filler.
_WANT = {
    "pref_weather": {
        "warm": ["WARM WEATHER", "SUNSHINE YEAR-ROUND", "NO REAL WINTER", "BEACH WEATHER"],
        "cold": ["COLD WINTERS", "REAL SEASONS", "SNOW ON CAMPUS", "SWEATER WEATHER"],
        "mild": ["EASY WEATHER", "MILD WINTERS", "NEVER TOO COLD"],
    },
    "pref_sports": {
        "strong": ["BIG-TIME SPORTS", "GAMEDAY CULTURE", "A REAL SPORTS SCENE",
                   "PACKED FOOTBALL SATURDAYS", "D1 SPORTS"],
        "low": ["A CHILL SPORTS SCENE", "SPORTS DON'T RUN THE SCHOOL",
                "LOW-KEY ON SPORTS"],
    },
    "pref_greek": {
        "strong": ["A BIG GREEK SCENE", "FRATS & SORORITIES", "GREEK LIFE",
                   "A REAL GREEK CULTURE"],
        "avoid": ["NO GREEK LIFE", "ZERO FRAT CULTURE", "NO RUSH"],
    },
    "pref_size": {
        "large": ["A HUGE CAMPUS", "A MASSIVE STUDENT BODY", "A BIG STATE-SCHOOL FEEL"],
        "ml": ["A BIG CAMPUS", "A LARGE STUDENT BODY", "PLENTY OF PEOPLE TO MEET"],
        "medium": ["A MID-SIZE CAMPUS", "NOT TOO BIG, NOT TOO SMALL"],
        "small": ["A SMALL CAMPUS", "SMALLER CLASSES", "A TIGHTER COMMUNITY"],
        "xs": ["A TINY CAMPUS", "TINY CLASSES", "A CLOSE-KNIT COMMUNITY"],
    },
    "pref_setting": {
        "urban": ["CITY LIFE", "A BIG-CITY CAMPUS", "EVERYTHING WALKABLE",
                  "AN URBAN CAMPUS"],
        "college_town": ["A CLASSIC COLLEGE TOWN", "A TRUE COLLEGE-TOWN VIBE",
                         "A TOWN BUILT AROUND THE SCHOOL"],
        "suburban": ["A SUBURBAN CAMPUS", "A QUIET, SAFE AREA"],
        "rural": ["A RURAL CAMPUS", "WIDE-OPEN SPACE", "OUT IN NATURE"],
    },
}
_WANT_ORDER = ["pref_sports", "pref_greek", "pref_weather", "pref_setting", "pref_size"]

# Major / program family -> a concrete academic "want" the student is after. Lets
# the title say something real about academics (a strong CS program, great pre-med)
# instead of only lifestyle vibes. Matched loosely against the seed school's major.
_MAJOR_WANTS = [
    (("computer", "cs", "software", "data"), ["A STRONG CS PROGRAM", "A TOP CS SCHOOL",
                                              "A SERIOUS TECH SCENE"]),
    (("engineer",), ["A TOP ENGINEERING SCHOOL", "A STRONG ENGINEERING PROGRAM"]),
    (("business", "finance", "account", "economic", "manage"),
     ["A STRONG BUSINESS SCHOOL", "REAL WALL-STREET PIPELINE", "A TOP BUSINESS PROGRAM"]),
    (("bio", "pre-med", "premed", "health", "nurs", "neuro"),
     ["GREAT PRE-MED", "A STRONG PRE-MED TRACK", "A REAL RESEARCH SCENE"]),
    (("art", "design", "music", "film", "theat", "media"),
     ["A SERIOUS ARTS SCENE", "A STRONG ARTS PROGRAM"]),
    (("polit", "law", "internation", "government", "public"),
     ["A STRONG POLI-SCI PROGRAM", "A PIPELINE INTO LAW & POLICY"]),
    (("psych", "sociol", "english", "history", "philos"),
     ["STRONG LIBERAL ARTS", "GREAT HUMANITIES"]),
]


def _major_want(major):
    """A concrete program want for a major string, or a generic 'STRONG <MAJOR>'."""
    m = (major or "").lower()
    if not m:
        return None
    for keys, phrasings in _MAJOR_WANTS:
        if any(k in m for k in keys):
            return random.choice(phrasings)
    return f"A STRONG {major.upper()} PROGRAM"


def title_wants(prefs, n=4, rng=random):
    """Short ALL-CAPS 'wants' tokens for the title slide (one per line), e.g.
    ['GAMEDAY CULTURE', 'GREEK LIFE', 'SUNSHINE YEAR-ROUND', 'A STRONG CS PROGRAM'].

    Pulls every want the prefs justify (so each token is a real attribute of the
    same school the reveal lands on — never a promise the reveal contradicts), picks
    a random concrete phrasing per category, and shuffles the order so the same seed
    doesn't always lead with the same two. Returns up to n."""
    pool = []
    for k in _WANT_ORDER:
        v = prefs.get(k)
        opts = _WANT.get(k, {}).get(v)
        if opts:
            pool.append(rng.choice(opts))
    mw = _major_want(prefs.get("major"))
    if mw:
        pool.append(mw)
    rng.shuffle(pool)
    return pool[:n]


def dream_for(fit_slug, fit_school, A, rng=random):
    """A higher-prestige 'dream school' that contrasts the My Fit pick (different
    climate / region / setting) so the reveal has tension."""
    fc = A.climate_of(fit_school)
    fr = A.region_of(fit_school)
    fs = A.setting_of(fit_school)
    fit_acc = fit_school.get("accept") or 0.2
    cands = []
    for s in DREAM_NAMES:
        if s == fit_slug:
            continue
        sc = A.COLLEGES_BY_SLUG.get(s)
        if not sc:
            continue
        scm = A.merged_school(sc)
        harder = (scm.get("accept") or 1) <= fit_acc + 0.02
        different = (A.climate_of(scm) != fc or A.region_of(scm) != fr or A.setting_of(scm) != fs)
        if harder and different:
            cands.append(s)
    if not cands:
        cands = [s for s in DREAM_NAMES if s != fit_slug
                 and (A.COLLEGES_BY_SLUG.get(s, {}).get("accept") or 1) <= fit_acc + 0.02]
    return rng.choice(cands) if cands else None


HEADERS = {
    "fit": ["THIS STUDENT WANTS...", "THEY WANT:", "WHAT THEY ACTUALLY WANT:"],
    "dream": ["BUT THEY ACTUALLY WANT:", "WHAT THEY'RE REALLY AFTER:", "THEY ACTUALLY WANT:"],
}
