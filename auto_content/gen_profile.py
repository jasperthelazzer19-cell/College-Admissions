#!/usr/bin/env python3
"""Profile + title-copy generation for the Content Autopilot.

generate(slug) -> dict with a compelling LLM-written applicant profile (saved to
user 181) plus the chosen title formula (lines + which words get the school
accent color) and the slide-3 type. No rendering here.

Env: ANTHROPIC_KEY required.
"""
import os, sys, json, random, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import anthropic

_client = None
def _claude():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])
    return _client

SCHOOLS = sorted(set(app.INST_LOGOS) & set(app.COLLEGES_BY_SLUG))


# Proven @candor title patterns, grouped by the reveal slide they set up. Given
# to the model as INSPIRATION for the chosen reveal type — it varies wording and
# invents fresh angles in the same family, not just copies. Keeping a healthy
# spread per type (and letting the factory pick the type) is what keeps the feed
# trying every format instead of defaulting to "chances".
_TITLE_FAMILIES = {
    "chances": [
        "WOULD YOU | ADMIT | THIS STUDENT | TO {S}?   (accent: ADMIT, {S})",
        "GUESS THIS | STUDENT'S ODDS | OF GETTING | INTO {S}   (accent: {S})",
        "CANDOR TOLD ME | IF THIS | {S} | APPLICANT | COULD GET IN   (accent: {S})",
        "I THOUGHT | THIS STUDENT | WOULD GET | INTO {S}...   (accent: {S})",
        "DID THIS | APPLICANT | GET INTO | {S}?   (accent: {S})",
        "BE HONEST — | DOES THIS KID | GET INTO | {S}?   (accent: HONEST, {S})",
        "REJECTED OR | ACCEPTED | AT {S}?   (accent: REJECTED, {S})",
        "ACCEPTED OR | REJECTED | AT {S}?   (accent: ACCEPTED, {S})",
        "CAN I | GET INTO | {S}?   (accent: {S})",
        "THIS STUDENT THINKS | THEY'RE GETTING | INTO {S} — | ARE THEY RIGHT?   (accent: {S}, RIGHT)",
        # brand-forward "Candor ___" hooks (no number — reveal is a later slide)
        "CANDOR GAVE | THIS {S} | APPLICANT A...   (accent: {S})",
        "CANDOR SAYS | THIS {S} | APPLICANT...   (accent: {S})",
        "I ASKED CANDOR | IF THIS KID | GETS INTO | {S}   (accent: CANDOR, {S})",
        "CANDOR RAN | THE ODDS ON | THIS {S} | APPLICANT   (accent: {S})",
        "CANDOR PREDICTED | THIS {S} | DECISION   (accent: {S})",
        # more guessing-game / verdict framings
        "IN OR OUT | AT {S}?   (accent: IN, {S})",
        "DOES THIS | APPLICANT | CRACK {S}?   (accent: CRACK, {S})",
        "WOULD {S} | TAKE THIS | APPLICANT?   (accent: {S})",
        "IS THIS | A {S} | ADMIT?   (accent: {S})",
        "WHAT ARE | THE ODDS | THIS KID | GETS INTO | {S}?   (accent: {S})",
        "HOW LIKELY | IS THIS KID | TO GET INTO | {S}?   (accent: {S})",
        "PREDICT THIS | APPLICANT'S | {S} | DECISION   (accent: {S})",
        "{S}: | YES OR NO | FOR THIS | APPLICANT?   (accent: {S}, YES)",
        "IS THIS | ENOUGH | TO GET INTO | {S}?   (accent: {S})",
        # underdog / overconfidence / spicy angles
        "EVERYONE SAYS | THIS KID CAN'T | GET INTO | {S}   (accent: CAN'T, {S})",
        "THIS KID SWEARS | THEY'RE GETTING | INTO {S}   (accent: {S})",
        "NO WAY | THIS APPLICANT | GETS INTO | {S}... | RIGHT?   (accent: {S})",
        "THEY THINK | THEY'RE A LOCK | FOR {S}   (accent: LOCK, {S})",
    ],
    "grade": [
        "CANDOR | GRADED THIS | {S} | APPLICANT'S | PROFILE   (accent: {S})",
        "CAN YOU | BEAT THIS | {S} | APPLICANT?   (accent: BEAT THIS, {S})",
        "WHAT'S THIS | {S} | APPLICANT'S | BIGGEST | WEAKNESS?   (accent: {S}, WEAKNESS)",
        "CAN AI | PREDICT | {S} | ADMISSIONS?   (accent: AI, {S})",
        "RATE THIS | {S} | APPLICANT | OUT OF 100   (accent: {S})",
        "GREEN FLAG OR | RED FLAG | FOR {S}?   (accent: RED FLAG, {S})",
        "HOW STRONG | IS THIS | {S} | APPLICANT, | REALLY?   (accent: {S})",
        # brand-forward "Candor ___" hooks
        "CANDOR | SCORED THIS | {S} | APPLICANT   (accent: {S})",
        "CANDOR | RATED THIS | {S} | PROFILE   (accent: {S})",
        "CANDOR | BROKE DOWN | THIS {S} | PROFILE   (accent: {S})",
        "WATCH CANDOR | GRADE THIS | {S} | APPLICANT   (accent: CANDOR, {S})",
        # more rate / roast / hot-take framings
        "GRADE THIS | {S} | APPLICANT | /100   (accent: {S})",
        "HOW COOKED | IS THIS | {S} | APPLICANT?   (accent: COOKED, {S})",
        "IS THIS | A STRONG | {S} | APPLICANT?   (accent: STRONG, {S})",
        "HOW MID | IS THIS | {S} | APPLICANT?   (accent: MID, {S})",
        "ROAST THIS | {S} | APPLICATION   (accent: ROAST, {S})",
        "ARE YOU | STRONGER THAN | THIS {S} | APPLICANT?   (accent: {S})",
        "WHAT WOULD YOU | RATE THIS | {S} | APPLICANT?   (accent: {S})",
        "WHAT'S THE | ONE THING | HOLDING THIS | {S} | APPLICANT BACK?   (accent: {S})",
    ],
    "glowup": [
        "WHAT WOULD IT TAKE | TO GET THIS | STUDENT INTO | {S}?   (accent: {S})",
        "HOW DOES THIS | APPLICANT | GET INTO | {S}?   (accent: {S})",
        "FIXING THIS | APPLICANT'S | SHOT AT | {S}   (accent: {S})",
        "WHAT THIS KID | NEEDS TO ADD | TO CRACK | {S}   (accent: {S})",
        "THE GLOW-UP | THIS APPLICANT | NEEDS FOR | {S}   (accent: GLOW-UP, {S})",
        # brand-forward "Candor ___" hooks
        "CANDOR'S PLAN | TO GET THIS KID | INTO {S}   (accent: {S})",
        "CANDOR SHOWS | HOW TO CRACK | {S}   (accent: {S})",
        "HOW CANDOR | WOULD FIX | THIS {S} | APPLICATION   (accent: {S})",
        # more "what's missing / how to get in" framings
        "WHAT'S MISSING | FROM THIS | {S} | APPLICATION?   (accent: {S})",
        "ONE SPIKE AWAY | FROM {S}?   (accent: {S})",
        "HOW TO TURN | THIS INTO A | {S} | ADMIT   (accent: {S})",
        "WHAT THIS KID | SHOULD'VE DONE | FOR {S}   (accent: {S})",
        "FROM REJECT | TO {S} ADMIT — | WHAT CHANGES?   (accent: {S})",
        "WHAT WOULD | GET THIS KID | OVER THE LINE | AT {S}?   (accent: {S})",
    ],
}
# Flat list for backward-compat / when no reveal type is requested. Fold the
# slide3 tag into each pattern's "(accent: ...)" note → "(accent: ...; slide3: t)".
_TITLE_EXAMPLES = "\n".join(
    (f"{ex.rstrip()[:-1]}; slide3: {t})" if ex.rstrip().endswith(")") else f"{ex}   (slide3: {t})")
    for t, exs in _TITLE_FAMILIES.items() for ex in exs)


# Local corpus of ~1,100 REAL r/collegeresults applicant posts (full self-text).
# We seed each generated profile from a random real one so the feed reflects
# actual applicants — every strength level + archetype — instead of the model's
# samey invented superstar. We ignore which schools they applied to/got into;
# only their real stats + activities matter (the carousel's school is chosen
# separately). This also keeps odds honest, since the profiles aren't inflated.
_REAL_POSTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "reddit_results_raw.json")
_REAL_POSTS = None


def _load_real_posts():
    global _REAL_POSTS
    if _REAL_POSTS is None:
        try:
            data = json.load(open(_REAL_POSTS_PATH))
            _REAL_POSTS = [p for p in data
                           if len((p.get("self") or "")) > 400
                           and any(k in ((p.get("self", "") + p.get("title", "")).lower())
                                   for k in ("gpa", "sat", "act"))]
        except Exception as e:
            print("real-posts load:", e); _REAL_POSTS = []
    return _REAL_POSTS


# Cycle through every real applicant once before repeating any: persist the set
# of seed indices already used, draw only from the unused, and reset once the
# whole corpus (~1,000) has been used.
_SEED_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seed_state.json")


def _pick_seed_post():
    posts = _load_real_posts()
    if not posts:
        return None
    try:
        used = set(json.load(open(_SEED_STATE)).get("used", []))
    except Exception:
        used = set()
    avail = [i for i in range(len(posts)) if i not in used]
    if not avail:                      # cycled through the whole corpus -> start over
        used, avail = set(), list(range(len(posts)))
        print(f"seed corpus exhausted ({len(posts)}) — resetting", flush=True)
    i = random.choice(avail)
    used.add(i)
    try:
        json.dump({"used": sorted(used), "total": len(posts)}, open(_SEED_STATE, "w"))
    except Exception as e:
        print("seed-state save:", e)
    body = (posts[i].get("self") or "").strip()
    return body[:2000]


def _seed_block(post):
    if not post:
        return ""
    return (
        "\n\nSEED — here is a REAL r/collegeresults applicant's own write-up. Build the "
        "profile from THEIR actual stats and activities:\n\"\"\"\n" + post + "\n\"\"\"\n"
        "- Use their REAL GPA, test scores, AP count/rigor, intended major, state, and their "
        "ACTUAL extracurriculars/awards. Keep the numbers and EC strength FAITHFUL — do not "
        "inflate them into a superstar, and don't water a strong one down.\n"
        "- IGNORE which colleges they applied to or got into — that's irrelevant here.\n"
        "- Reformat into the schema: 7-9 of their real ECs (short, punchy), their real awards, "
        "their real stats. If a field isn't stated, infer something plausible and consistent "
        "with the rest of their profile. The result should read like THIS real applicant.")


def _gen_profile_and_title(slug, want_slide3=None, seed=None):
    sch = app.COLLEGES_BY_SLUG[slug]
    name = sch.get("name")
    short, _ = app._school_brand(slug, name)
    accept = sch.get("accept") or 0.15
    # When the factory asks for a specific reveal type, show ONLY that family's
    # patterns and lock slide3 — so the hook always matches the slide we'll show
    # (no more "grade" reveal under a "chances" hook), and every format gets used.
    if want_slide3 in _TITLE_FAMILIES:
        examples = "\n".join(_TITLE_FAMILIES[want_slide3]).replace("{S}", short)
        slide3_instr = (f'slide3 type: ALWAYS use "{want_slide3}" — write the hook in that family only '
                        f'(the patterns below are all {want_slide3} hooks).')
    else:
        examples = _TITLE_EXAMPLES.replace("{S}", short)
        slide3_instr = ('slide3 type: "grade" for grading/score/profile hooks, "glowup" for '
                        '"what would it take / how to get in / glow up" hooks, else "chances".')
    prompt = f"""You write content for a college-admissions TikTok (@candor). Produce TWO things for
{name} (acceptance ~{round(accept*100)}%): a realistic applicant profile, and a punchy title hook.

PROFILE — invent ONE realistic, specific, COMPELLING U.S. high-school senior with a coherent "spike"
(not generic). Vary the archetype run to run (STEM researcher, humanities writer, athlete-scholar,
founder, artist, debater, etc.).
- Stats believable for a competitive {name} applicant, NOT robotically perfect.
- EXACTLY 7-9 extracurriculars, each SHORT/punchy: title + ONE metric in parens, 3-8 words MAX.
  GOOD: "Founder, Urban Mapping Project (140+ sites)" / "Varsity Track Captain". BAD: sentences, em-dashes.
- 1-3 awards under 8 words each. Major tied to the spike. Full-name state. No em-dashes.

TITLE — a hook for slide 1, broken into 4-5 SHORT all-caps lines (each line scaled to fill the width).
CRITICAL: it MUST stay in the @candor "guessing-game" framing — the hook makes the viewer GUESS the
applicant's odds, or react to a grade/verdict on the applicant. Vary the WORDING and which pattern you
use (you may lightly remix phrasing), but DO NOT write a narrative sentence describing the student's
achievements (e.g. NOT "SHE STARTED A WATER MONITORING NETWORK"). It must read like one of these
families: "would you admit", "guess the odds", "candor told me if", "can you beat", "biggest weakness",
"I thought they'd get in", "can AI predict", "what would it take to get in" (glow-up), OR a brand-forward
"CANDOR ___ this applicant" line — e.g. CANDOR GAVE / CANDOR SAID / CANDOR GRADED / CANDOR SCORED /
CANDOR RATED / CANDOR PREDICTED / CANDOR RAN THE ODDS / I ASKED CANDOR. LEAN INTO leading the hook with
"CANDOR" often (it promotes the brand) — but still vary so the feed isn't every single one identical.
NEVER put a specific number or percentage in the title (you do NOT know the real odds/grade;
those are revealed on a later slide). No "%", no made-up scores.
VARIETY: about a third of the time, instead of the generic "THIS STUDENT/APPLICANT", name the
applicant by their SPIKE for a sharper hook — e.g. "THIS PUBLISHED RESEARCHER", "THIS RECRUITED
ATHLETE", "THIS CS APPLICANT", "THIS NONPROFIT FOUNDER", "THIS 1590 APPLICANT". Keep it short
and true to the profile you wrote; still keep the guessing-game/verdict framing.
{slide3_instr}
The school short name is "{short}". Pick which words get the school's accent color (always the school
name; optionally one emphasis word).
Patterns (| separates lines):
{examples}
{_seed_block(seed)}
Return ONLY strict JSON:
{{"profile": {{"uw_gpa": float, "weighted_gpa": float, "sat": int|null, "act": int|null,
"sat_math": int|null, "sat_ebrw": int|null, "major": str, "state": str, "school_type": "public"|"private",
"aps": str, "ecs": str, "leadership": str, "awards": str}},
"title": {{"lines": ["LINE 1", "LINE 2", ...], "accent_words": ["{short}", ...], "slide3": "chances"|"grade"|"glowup"}}}}"""
    # Route through app._claude so the profile+hook generation inherits the
    # USE_MAX_CLI (free Max-plan) path + automatic API fallback, same as the
    # reveal-slide bullets.
    txt = app._claude("claude-haiku-4-5-20251001", None, prompt, max_tokens=1300, temperature=1.0)
    if not txt:
        raise RuntimeError("gen_profile: no LLM output")
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1].lstrip("json").strip()
    return json.loads(txt)


def _normalize_list_field(v):
    """ECs/awards/leadership must be ONE PER LINE for the profile slide. The model
    sometimes returns them semicolon-joined on one line — split those out."""
    import re
    parts = re.split(r"[\n;|]+", str(v or ""))   # split on newline, semicolon, OR pipe
    return "\n".join(p.strip(" -•\t") for p in parts if p.strip())


def _ensure_user(uid):
    """profiles has a FK to users(id); make sure the slot exists (181 = creator,
    38 = the head-to-head 'Student B')."""
    with app.db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (id,email,password_hash,password_salt) "
                     "VALUES (?,?,?,?)", (uid, f"autogen{uid}@candor.local", "x", "y"))
        conn.commit()


def generate(slug=None, uid=181, want_slide3=None):
    slug = slug if slug in app.COLLEGES_BY_SLUG else random.choice(SCHOOLS)
    sch = app.COLLEGES_BY_SLUG[slug]
    seed = _pick_seed_post()                # a random REAL applicant write-up (or None)
    d = _gen_profile_and_title(slug, want_slide3=want_slide3, seed=seed)
    profile = d["profile"]
    for f in ("ecs", "leadership", "awards"):
        profile[f] = _normalize_list_field(profile.get(f))
    # Put the GPA on the 4.0 scale before it's saved AND before it's rendered on the
    # carousel, so a weighted/100-point value never shows up raw on a TikTok slide.
    if profile.get("uw_gpa") not in (None, ""):
        profile["uw_gpa"] = app.normalize_gpa(profile["uw_gpa"])
    import time as _t
    for _attempt in range(4):                       # retry transient SQLite IO errors
        try:
            _ensure_user(uid)
            app.save_profile(uid, profile)
            break
        except Exception as _e:
            if _attempt == 3:
                raise
            _t.sleep(1.5)
    # Grade with the best judge available (free Max-plan CLI when CLAUDE_CLI=1,
    # else the Anthropic API) and persist a realistic ec_rating + exceptional
    # verdict, so odds aren't inflated by the keyword grader. Only no-ops when
    # neither judge is reachable (no CLI and no API key).
    try:
        app.autopilot_grade_profile(uid, profile)
    except Exception as _e:
        print("autopilot grade skipped:", _e)
    short, _ = app._school_brand(slug, sch.get("name"))
    t = d["title"]
    # Lock to the requested reveal type when the factory asked for one (the hook
    # was written in that family); else trust the model's pick.
    slide3 = want_slide3 if want_slide3 in _TITLE_FAMILIES else t.get("slide3", "chances")
    # Enforce the "no number/percentage in the title" rule the prompt asks for —
    # the reveal (odds %, grade) lives on a later slide; a leaked number both
    # spoils the guessing-game and is an LLM hallucination, not the real value.
    lines = [ln for ln in t["lines"] if not re.search(r"\d", ln)] or t["lines"]
    return {"slug": slug, "name": sch.get("name"), "short": short, "profile": profile,
            "lines": lines, "accent_words": t.get("accent_words", [short]),
            "slide3": slide3}


if __name__ == "__main__":
    out = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({k: out[k] for k in ("slug", "name", "lines", "accent_words", "slide3")}))
