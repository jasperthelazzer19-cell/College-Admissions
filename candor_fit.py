"""Candor "Where should they go?" fit engine.

Powers the best-fit content angle (lifestyle/vibe instead of admit-odds). Pure:
every function takes a slug + the school dict (from app.COLLEGES_BY_SLUG) and
returns derived attributes / trait copy — no app import, no DB, easy to test.

The carousel is reverse-engineered: pick the best-fit school FIRST, derive its
real vibe (region/climate from state, size bucket from size, curated sports/greek/
setting), then write 4-5 lifestyle traits that TRUE-describe it. That makes the
reveal feel earned and gives commenters something to argue about
("Wisconsin fits those traits better", "Michigan clears").
"""
import random

# ── climate by state (content axis: cold winters vs warm/sunny) ──────────────
_COLD = {"Massachusetts", "Rhode Island", "Connecticut", "New York", "New Hampshire",
         "Vermont", "Maine", "New Jersey", "Pennsylvania", "Michigan", "Wisconsin",
         "Minnesota", "Illinois", "Indiana", "Ohio", "Iowa", "Nebraska", "North Dakota",
         "South Dakota", "Montana", "Wyoming", "Alaska"}
_WARM = {"California", "Florida", "Georgia", "Texas", "Louisiana", "Alabama",
         "Mississippi", "South Carolina", "Arizona", "New Mexico", "Nevada", "Hawaii",
         "Tennessee", "Oklahoma", "Arkansas"}
# everything else (NC, VA, MD, DC, WA, OR, MO, KY, CO, UT, ...) reads as "mild"

# ── region phrasing by state ─────────────────────────────────────────────────
_REGION = {
    "Massachusetts": "the Northeast", "Rhode Island": "the Northeast",
    "Connecticut": "the Northeast", "New York": "the Northeast",
    "New Hampshire": "the Northeast", "Vermont": "the Northeast",
    "Maine": "the Northeast", "New Jersey": "the Northeast",
    "Pennsylvania": "the Northeast",
    "Michigan": "the Midwest", "Wisconsin": "the Midwest", "Illinois": "the Midwest",
    "Indiana": "the Midwest", "Ohio": "the Midwest", "Missouri": "the Midwest",
    "Minnesota": "the Midwest", "Iowa": "the Midwest",
    "Florida": "the South", "Georgia": "the South", "Texas": "the South",
    "Louisiana": "the South", "North Carolina": "the South", "Virginia": "the South",
    "Tennessee": "the South", "South Carolina": "the South", "Alabama": "the South",
    "California": "the West Coast", "Washington": "the West Coast",
    "Oregon": "the West Coast", "Colorado": "the West", "Arizona": "the Southwest",
    "Maryland": "the Mid-Atlantic", "District of Columbia": "the Mid-Atlantic",
}

# ── curated vibe membership (slug-keyed; only needs the renderable ~50) ───────
BIG_SPORTS = {"umich", "wisc", "notre-dame", "usc", "ut-austin", "uf", "miami", "unc",
              "ucla", "duke", "gatech", "uiuc", "umd", "purdue", "uw", "uva", "villanova"}
GREEK = {"usc", "vanderbilt", "wake-forest", "tulane", "uva", "ut-austin", "uf",
         "miami", "duke", "villanova", "wisc", "umich", "unc"}
URBAN = {"nyu", "bu", "northeastern", "columbia", "usc", "ucla", "gatech", "georgetown",
         "uchicago", "upenn", "jhu", "cmu", "tulane"}
COLLEGE_TOWN = {"cornell", "dartmouth", "williams", "amherst", "umich", "unc", "uva",
                "uf", "wisc", "uiuc", "notre-dame", "ut-austin", "duke"}
# everything else falls back to "suburban"

# schools that read as a "dream / reach" name on TikTok (Format 2 contrast)
DREAM_NAMES = ["harvard", "stanford", "mit", "yale", "princeton", "columbia", "upenn",
               "brown", "duke", "uchicago", "cornell", "dartmouth", "ucla", "ucb", "nyu"]


def _climate(state):
    if state in _COLD:
        return "cold"
    if state in _WARM:
        return "warm"
    return "mild"


def _size_bucket(size):
    size = size or 0
    if size >= 20000:
        return "large"
    if size >= 7000:
        return "mid"
    return "small"


def _setting(slug):
    if slug in URBAN:
        return "urban"
    if slug in COLLEGE_TOWN:
        return "college-town"
    return "suburban"


def vibe(slug, school):
    """Derived attributes for one school (mix of real fields + curated tags)."""
    state = (school.get("state") or "").strip()
    majors = school.get("majors") or []
    return {
        "slug": slug,
        "state": state,
        "region": _REGION.get(state, "a great spot"),
        "climate": _climate(state),
        "size": school.get("size") or 0,
        "size_bucket": _size_bucket(school.get("size")),
        "public": (school.get("type") == "public"),
        "elite": (school.get("tier") or 9) <= 2,
        "top_major": majors[0] if majors else None,
        "sports": slug in BIG_SPORTS,
        "greek": slug in GREEK,
        "setting": _setting(slug),
    }


# trait phrasings per attribute (a few variants each so carousels don't repeat)
_PHRASES = {
    "sports": ["Big-time college sports", "A huge sports culture", "Saturday football energy",
               "Real game-day atmosphere"],
    "greek": ["A big Greek life scene", "Frats and sororities", "Strong Greek life"],
    "spirit": ["Tons of school spirit", "A school everyone's proud of"],
    "cold": ["Cold winters and real seasons", "Snowy, four-season winters", "Actual fall and winter"],
    "warm": ["Warm, sunny weather", "Sunshine basically year-round", "Warm-weather campus life"],
    "mild": ["Mild, easy weather", "Nice weather most of the year"],
    "large": ["A big-school feel", "A huge campus", "Tens of thousands of students"],
    "mid": ["A mid-sized campus", "Not too big, not too small"],
    "small": ["A small, tight-knit campus", "Small classes and a close community"],
    "public": ["A big public-school vibe"],
    "urban": ["A real city campus", "City life right outside the door"],
    "college-town": ["A classic college town", "A town that lives for the school"],
    "suburban": ["A pretty, self-contained campus"],
    "elite": ["Seriously strong academics", "Top-tier academics"],
}


def traits(slug, school, n=5, rng=random):
    """4-5 punchy lifestyle traits that TRUE-describe the school, ordered most-
    distinctive first so the best-fit reveal feels earned."""
    v = vibe(slug, school)
    buckets = []
    if v["sports"]:
        buckets.append("sports")
    if v["climate"] in ("cold", "warm"):
        buckets.append(v["climate"])
    if v["greek"]:
        buckets.append("greek")
    if v["top_major"]:
        buckets.append("major")
    buckets.append(v["size_bucket"])
    if v["sports"] or v["public"]:
        buckets.append("spirit")
    buckets.append(v["setting"])
    if v["elite"]:
        buckets.append("elite")
    if v["climate"] == "mild":
        buckets.append("mild")
    if v["public"]:
        buckets.append("public")

    out, seen = [], set()
    for b in buckets:
        if b in seen:
            continue
        seen.add(b)
        if b == "major":
            txt = rng.choice([f"A top {v['top_major']} program", f"Strong {v['top_major']}"])
        else:
            txt = rng.choice(_PHRASES[b])
        out.append(txt)
        if len(out) >= n:
            break
    return out[:n]


def dream_for(fit_slug, fit_school, colleges_by_slug, rng=random):
    """Pick a higher-prestige 'dream school' that contrasts with the best fit —
    a reach name whose vibe differs (different climate/region or setting), so the
    'but Candor thinks their best fit is...' reveal creates real tension."""
    fv = vibe(fit_slug, fit_school)
    fit_acc = fit_school.get("accept") or 0.2
    cands = []
    for s in DREAM_NAMES:
        if s == fit_slug:
            continue
        sc = colleges_by_slug.get(s)
        if not sc:
            continue
        dv = vibe(s, sc)
        harder = (sc.get("accept") or 1) <= fit_acc + 0.02   # at least as selective
        different = (dv["climate"] != fv["climate"] or dv["region"] != fv["region"]
                     or dv["setting"] != fv["setting"])
        if harder and different:
            cands.append(s)
    if not cands:                       # fall back to any harder reach name
        cands = [s for s in DREAM_NAMES if s != fit_slug
                 and (colleges_by_slug.get(s, {}).get("accept") or 1) <= fit_acc + 0.02]
    return rng.choice(cands) if cands else None


# header copy for the trait slide, by variant
HEADERS = {
    "fit": ["THIS STUDENT WANTS...", "THEY WANT:", "WHAT THEY ACTUALLY WANT:"],
    "dream": ["BUT THEY ACTUALLY WANT:", "WHAT THEY'RE REALLY AFTER:", "THEY ACTUALLY WANT:"],
}
