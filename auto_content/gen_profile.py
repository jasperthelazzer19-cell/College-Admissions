#!/usr/bin/env python3
"""Profile + title-copy generation for the Content Autopilot.

generate(slug) -> dict with a compelling LLM-written applicant profile (saved to
user 181) plus the chosen title formula (lines + which words get the school
accent color) and the slide-3 type. No rendering here.

Env: ANTHROPIC_KEY required.
"""
import os, sys, json, random
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


# Proven @candor title patterns — given to the model as INSPIRATION. It is told
# to vary the wording and invent fresh angles, not just copy these.
_TITLE_EXAMPLES = """WOULD YOU | ADMIT | THIS STUDENT | TO {S}?   (accent: ADMIT, {S}; slide3: chances)
GUESS THIS | STUDENT'S ODDS | OF GETTING | INTO {S}   (accent: {S}; slide3: chances)
CANDOR TOLD ME | IF THIS | {S} | APPLICANT | COULD GET IN   (accent: {S}; slide3: chances)
CANDOR | GRADED THIS | {S} | APPLICANT'S | PROFILE   (accent: {S}; slide3: grade)
CANDOR | GRADED THIS | {S} | APPLICANT'S | PROFILE   (accent: {S}; slide3: grade)
CAN YOU | BEAT THIS | {S} | APPLICANT?   (accent: BEAT THIS, {S}; slide3: grade)
WHAT'S THIS | {S} | APPLICANT'S | BIGGEST | WEAKNESS?   (accent: {S}, WEAKNESS; slide3: grade)
I THOUGHT | THIS STUDENT | WOULD GET | INTO {S}...   (accent: {S}; slide3: chances)
CAN AI | PREDICT | {S} | ADMISSIONS?   (accent: AI, {S}; slide3: grade)
WHAT WOULD IT TAKE | TO GET THIS | STUDENT INTO | {S}?   (accent: {S}; slide3: glowup)"""


def _gen_profile_and_title(slug):
    sch = app.COLLEGES_BY_SLUG[slug]
    name = sch.get("name")
    short, _ = app._school_brand(slug, name)
    accept = sch.get("accept") or 0.15
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
"I thought they'd get in", "can AI predict", "candor graded this profile", "what would it take to get in"
(glow-up). NEVER put a specific number or percentage in the title (you do NOT know the real odds/grade;
those are revealed on a later slide). No "%", no made-up scores.
slide3 type: "grade" for grading/score/profile hooks, "glowup" for "what would it take / how to get in /
glow up" hooks, else "chances".
The school short name is "{short}". Pick which words get the school's accent color (always the school
name; optionally one emphasis word). Choose slide3 type: "grade" if about grading/score/profile, else "chances".
Patterns (| separates lines):
{_TITLE_EXAMPLES.replace("{S}", short)}

Return ONLY strict JSON:
{{"profile": {{"uw_gpa": float, "weighted_gpa": float, "sat": int|null, "act": int|null,
"sat_math": int|null, "sat_ebrw": int|null, "major": str, "state": str, "school_type": "public"|"private",
"aps": str, "ecs": str, "leadership": str, "awards": str}},
"title": {{"lines": ["LINE 1", "LINE 2", ...], "accent_words": ["{short}", ...], "slide3": "chances"|"grade"}}}}"""
    msg = _claude().messages.create(model="claude-haiku-4-5-20251001", max_tokens=1300,
                                    temperature=1.0, messages=[{"role": "user", "content": prompt}])
    txt = msg.content[0].text.strip()
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


def generate(slug=None, uid=181):
    slug = slug if slug in app.COLLEGES_BY_SLUG else random.choice(SCHOOLS)
    sch = app.COLLEGES_BY_SLUG[slug]
    d = _gen_profile_and_title(slug)
    profile = d["profile"]
    for f in ("ecs", "leadership", "awards"):
        profile[f] = _normalize_list_field(profile.get(f))
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
    short, _ = app._school_brand(slug, sch.get("name"))
    t = d["title"]
    return {"slug": slug, "name": sch.get("name"), "short": short, "profile": profile,
            "lines": t["lines"], "accent_words": t.get("accent_words", [short]),
            "slide3": t.get("slide3", "chances")}


if __name__ == "__main__":
    out = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({k: out[k] for k in ("slug", "name", "lines", "accent_words", "slide3")}))
