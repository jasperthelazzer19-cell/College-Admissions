#!/usr/bin/env python3
"""Candor Content Autopilot — generator/renderer (runs on the Mac).

Per carousel:
  1. LLM writes a compelling applicant profile + a varied title hook (cheap Haiku call)
  2. Slide 1 (title)  -> gpt-image-1 (medium) in the @candor image style
  3. Slide 2 (profile) + Slide 3 (chances/grade) -> Candor app exports, screenshot with headless Chrome
  4. POST the finished carousel to the live Railway queue (/content/queue/push)
The cron there releases per slot; the creator approves on /content/today.

Env:
  CRON_KEY        required (matches Railway ADMIN_KEY/CRON_KEY) — to push
  ANTHROPIC_KEY   required — profile/title copy + the real grader
  OPENAI_KEY      required — gpt-image-1 title image
  TARGET_URL      push target (default https://admit.up.railway.app)
  BUFFER_TARGET   keep this many pending (default 12)
  LOCAL_PORT      render app port (default 5077)
  IMG_QUALITY     gpt-image-1 quality (default medium)
Usage: python3 auto_content/factory.py [--n N] [--dry]
"""
import os, sys, time, json, base64, random, subprocess, urllib.request, urllib.error, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CRON_KEY      = os.environ.get("CRON_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
OPENAI_KEY    = os.environ.get("OPENAI_KEY", "")
TARGET_URL    = os.environ.get("TARGET_URL", "https://admit.up.railway.app").rstrip("/")
BUFFER_TARGET = int(os.environ.get("BUFFER_TARGET", "12"))
LOCAL_PORT    = int(os.environ.get("LOCAL_PORT", "5077"))
LOCAL_URL     = f"http://127.0.0.1:{LOCAL_PORT}"
CHROME        = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
IMG_QUALITY   = os.environ.get("IMG_QUALITY", "medium")

import app                       # noqa: E402  shared DB + school data
from auto_content import gen_profile  # noqa: E402

SCHOOLS = sorted(set(app.INST_LOGOS) & set(app.COLLEGES_BY_SLUG))


import re  # noqa: E402


def mark_accents(lines, accent_words):
    """Wrap the accent words/phrases in *asterisks* inside each line so the HTML
    title colors them in the school's brand color (the LLM returns the accent
    words as a separate list, not inline)."""
    aw = sorted({w.strip().upper() for w in (accent_words or []) if w and w.strip()},
                key=len, reverse=True)
    out = []
    for ln in lines:
        s = ln.upper()
        for w in aw:
            pat = re.compile(r'(?<!\*)\b' + re.escape(w) + r'\b(?!\*)')
            s = pat.sub("*" + w + "*", s, count=1)
        out.append(s)
    return out


# ── all slides rendered FREE via headless Chrome (no image API) ────────────
# NOTE: do NOT add --user-data-dir — with --headless=new it writes the PNG but
# hangs on exit. Renders run sequentially, so the default profile is fine.
def _shot(out_path, url):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", "--window-size=1024,1536",
                    "--virtual-time-budget=6000", "--timeout=12000",
                    f"--screenshot={out_path}", url],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    with open(out_path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def render_slides(slug, slide3, lines):
    """All 3 slides as PNG data URLs, rendered FREE via headless Chrome against the
    local Candor app. Slide 1 = HTML title (per-line fill + HD cached logo),
    slides 2 & 3 = the Candor profile + chances/grade exports."""
    tmp = "/tmp/cren_factory"; os.makedirs(tmp, exist_ok=True)
    hook = urllib.parse.quote("\n".join(lines))
    s1 = _shot(f"{tmp}/s1.png", f"{LOCAL_URL}/title/export?rkey={CRON_KEY}&clean=1&slug={slug}&hook={hook}")
    s2 = _shot(f"{tmp}/s2.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1")
    s3routes = {"grade": "/grade/export", "glowup": f"/glowup/{slug}/export",
                "chances": f"/chances/{slug}/export"}
    s3path = s3routes.get(slide3, s3routes["chances"])
    s3 = _shot(f"{tmp}/s3.png", f"{LOCAL_URL}{s3path}?rkey={CRON_KEY}&clean=1")
    return s1, s2, s3


def odds_grade_text(slug, profile, want_grade=True):
    odds, grade, low, high, gnum = "", "", None, None, None
    try:
        merged = app.merged_school(app.COLLEGES_BY_SLUG[slug])
        fit, _ = app.compute_fit(profile, merged)
        low, high = app.estimate_odds(merged, fit, profile)
        odds = f"{low}–{high}% chance"
    except Exception as e:
        print("odds:", e)
    if want_grade:   # only compute the /100 grade when the slide actually shows it
        try:
            g = app._grade_cached(181, profile, compute=True)
            gnum = max(1, min(100, round(g['overall'] / 10)))
            grade = f"{gnum}/100"
        except Exception as e:
            print("grade:", e)
    return odds, grade, low, high, gnum


def numeric_title(short, slide3, low, high, gnum):
    """A deterministic reveal-title built from the REAL odds/grade (no LLM guess).
    Returns (lines, accent_words) or None if the value isn't available."""
    if slide3 == "grade" and gnum is not None:
        return (["CANDOR GRADED THIS", short, "APPLICANT", f"{gnum}/100"], [short, f"{gnum}/100"])
    if slide3 == "chances" and low is not None:
        return (["CANDOR GAVE THIS", short, "APPLICANT A...", f"{low}-{high}%", "CHANCE"], [short, f"{low}-{high}%"])
    return None


# ── plumbing ───────────────────────────────────────────────────────────────
def _get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def buffer_pending():
    try:
        return _get_json(f"{TARGET_URL}/content/queue/status?key={CRON_KEY}").get("pending", 0)
    except Exception as e:
        print("status check failed:", e); return 0


def push(payload):
    req = urllib.request.Request(f"{TARGET_URL}/content/queue/push?key={CRON_KEY}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


import threading  # noqa: E402
_server = None


def start_local_server():
    """Serve the Candor app IN THIS PROCESS (a daemon thread) so the generator and
    the render endpoints share ONE set of DB connections — no cross-process SQLite
    'disk I/O error'. Replaces spawning a separate app.py."""
    global _server
    from werkzeug.serving import make_server
    _server = make_server("127.0.0.1", LOCAL_PORT, app.app, threaded=True)
    threading.Thread(target=_server.serve_forever, daemon=True).start()


def stop_local_server():
    global _server
    if _server:
        try:
            _server.shutdown()
        except Exception:
            pass
        _server = None


def wait_local_app():
    for _ in range(45):
        try:
            if _get_json(f"{LOCAL_URL}/healthz").get("ok"):
                return True
        except Exception:
            time.sleep(1)
    return False


def _title_png(slug, lines, accent_words, nologo=False):
    hook = urllib.parse.quote("\n".join(mark_accents(lines, accent_words)))
    extra = "&nologo=1" if nologo else ""
    return _shot("/tmp/cren_factory/t.png",
                 f"{LOCAL_URL}/title/export?rkey={CRON_KEY}&clean=1&slug={slug}&hook={hook}{extra}")


def _finish(payload, dry, label):
    if dry:
        print(f"  [dry] {label}")
        return None, payload["school_name"]
    res = push(payload)
    print(f"  pushed #{res.get('id')} {label} | pending={res.get('pending')}")
    return (res.get("id") if res.get("ok") else None), payload["school_name"]


def make_single(dry=False, slug=None, slide3=None):
    slug = slug if slug in SCHOOLS else random.choice(SCHOOLS)
    # Pick the reveal type up front (unless text-to-make requested one) and steer
    # the hook to match — chances stays the staple, grade + glow-up get real
    # airtime so the feed cycles every format instead of defaulting to chances.
    if slide3 not in ("chances", "grade", "glowup"):
        slide3 = random.choices(["chances", "grade", "glowup"], weights=[5, 3, 2])[0]
    g = gen_profile.generate(slug, want_slide3=slide3)   # profile -> 181 + matching hook
    g["slide3"] = slide3
    odds, grade, low, high, gnum = odds_grade_text(slug, g["profile"],
                                                   want_grade=(g["slide3"] == "grade"))
    short, accent = app._school_brand(slug, g["name"])
    lines, accent_words = g["lines"], g["accent_words"]
    marked = mark_accents(lines, accent_words)
    s1, s2, s3 = render_slides(slug, g["slide3"], marked)
    hook = " ".join(lines)
    payload = dict(school_slug=slug, school_name=g["name"], accent=accent, title_text=hook,
                   title_formula="llm", slide3_type=g["slide3"], profile_json=json.dumps(g["profile"]),
                   odds_text=odds, grade_text=grade, img1=s1, img2=s2, img3=s3,
                   meta={"lines": lines, "accent_words": accent_words})
    return _finish(payload, dry, f"{slug} | {hook[:40]} | {g['slide3']} | {odds} {grade}")


def _accept(slug):
    return app.COLLEGES_BY_SLUG[slug].get("accept") or 0.15


def _band_of(anchor):
    """The other 2 compare schools, picked to sit in the SAME selectivity band as
    the anchor — so all 3 have close acceptance rates (comparing 3 reaches, or 3
    targets, not a 4% Ivy next to a 50% school). Window is ±50% relative with a
    4-point floor; falls back to the nearest-by-accept-rate if the band is thin."""
    a = _accept(anchor)
    window = max(0.04, min(0.12, a * 0.5))   # ±50% relative, 4pt floor, 12pt ceiling
    pool = [s for s in SCHOOLS if s != anchor and abs(_accept(s) - a) <= window]
    if len(pool) < 2:
        pool = sorted((s for s in SCHOOLS if s != anchor), key=lambda s: abs(_accept(s) - a))[:6]
    return random.sample(pool, 2)


def make_compare(dry=False):
    """One student 'applied to' several schools (the 'compare' content feature).
    Slide 1 = 'THIS STUDENT APPLIED TO A, B & C...' with each school in its own
    color + all logos; slide 3 = the ranking. The 3 schools are kept in the same
    acceptance-rate band so the comparison is apples-to-apples."""
    slug = random.choice(SCHOOLS)
    comp = [slug] + _band_of(slug)          # 3 schools, similar selectivity
    g = gen_profile.generate(slug)          # profile -> 181
    odds, _, _, _, _ = odds_grade_text(slug, g["profile"], want_grade=False)
    accent = app._school_brand(slug, g["name"])[1]
    tmp = "/tmp/cren_factory"; os.makedirs(tmp, exist_ok=True)
    s1 = _shot(f"{tmp}/c1.png", f"{LOCAL_URL}/title-compare/export?rkey={CRON_KEY}&clean=1&slugs={','.join(comp)}")
    s2 = _shot(f"{tmp}/c2.png", f"{LOCAL_URL}/profile-neutral/export?rkey={CRON_KEY}&clean=1")
    s3 = _shot(f"{tmp}/c3.png", f"{LOCAL_URL}/compare/export?rkey={CRON_KEY}&clean=1&slugs={','.join(comp)}")
    shorts = ", ".join(app._school_brand(s, app.COLLEGES_BY_SLUG[s].get('name'))[0] for s in comp)
    payload = dict(school_slug=slug, school_name=f"Compare: {shorts}", accent=accent,
                   title_text=f"THIS STUDENT APPLIED TO {shorts}", title_formula="compare",
                   slide3_type="compare", profile_json=json.dumps(g["profile"]),
                   odds_text=odds, grade_text="", img1=s1, img2=s2, img3=s3, meta={"compare": comp})
    return _finish(payload, dry, f"COMPARE {shorts}")


def make_h2h(dry=False):
    """Head-to-head: 4 slides — title, Student A profile, Student B profile, and
    the A-vs-B result for one school. Generates TWO profiles (181=A, 38=B)."""
    slug = random.choice(SCHOOLS)
    gA = gen_profile.generate(slug, uid=181)   # student A
    gen_profile.generate(slug, uid=38)         # student B
    short, accent = app._school_brand(slug, gA["name"])
    lines = ["WHICH STUDENT", "GETS INTO", f"{short}?"]
    tmp = "/tmp/cren_factory"; os.makedirs(tmp, exist_ok=True)
    s1 = _title_png(slug, lines, [short])
    s2 = _shot(f"{tmp}/h2.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181&label=STUDENT%20A")
    s3 = _shot(f"{tmp}/h3.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=38&label=STUDENT%20B")
    s4 = _shot(f"{tmp}/h4.png", f"{LOCAL_URL}/headtohead/{slug}/export?rkey={CRON_KEY}&clean=1")
    payload = dict(school_slug=slug, school_name=f"H2H: {gA['name']}", accent=accent,
                   title_text=" ".join(lines), title_formula="h2h", slide3_type="h2h",
                   profile_json=json.dumps(gA["profile"]), odds_text="", grade_text="",
                   img1=s1, img2=s2, img3=s3, img4=s4, meta={"head_to_head": True})
    return _finish(payload, dry, f"H2H {short}")


# ── daily head-to-head cap (they cost ~2-4x a normal carousel) ─────────────
H2H_DAILY_CAP = int(os.environ.get("H2H_DAILY_CAP", "2"))
_H2H_STATE = os.path.join(HERE, ".h2h_state.json")


def _h2h_state():
    import datetime
    today = datetime.date.today().isoformat()
    try:
        d = json.load(open(_H2H_STATE))
    except Exception:
        d = {}
    if d.get("date") != today:
        d = {"date": today, "count": 0}
    return d


def _h2h_today():
    return _h2h_state().get("count", 0)


def _bump_h2h():
    d = _h2h_state()
    d["count"] = d.get("count", 0) + 1
    try:
        json.dump(d, open(_H2H_STATE, "w"))
    except Exception:
        pass


def make_one(dry=False, slug=None, slide3=None, ctype=None):
    """Dispatcher. Explicit slug (text-to-make) -> single. Otherwise rotate types
    so the feed cycles through every Candor format, capping head-to-head at
    H2H_DAILY_CAP/day (they cost ~2-4x a normal carousel)."""
    if ctype is None:
        if slug:
            ctype = "single"
        else:
            ctype = random.choices(["single", "compare", "h2h"], weights=[6, 3, 3])[0]
            if ctype == "h2h" and _h2h_today() >= H2H_DAILY_CAP:
                ctype = random.choice(["single", "compare"])
    if ctype == "compare":
        return make_compare(dry)
    if ctype == "h2h":
        _bump_h2h()
        return make_h2h(dry)
    return make_single(dry, slug, slide3)


def text_carousels(items):
    """iMessage the creator one intro + a tappable link per carousel (file
    attachments are broken via AppleScript on macOS; links send fine).
    items = list of (cid, school_name)."""
    num = os.environ.get("PHONE", "")
    if not num or not items:
        print("PHONE not set or nothing to text — skipping"); return False
    intro = (f"\U0001F3AC {len(items)} new Candor carousel option(s) — tap each, "
             f"long-press the slides to save:")
    sends = [f' send "{intro}" to b']
    for cid, school in items:
        link = f"{TARGET_URL}/content/c/{cid}?key={urllib.parse.quote(CRON_KEY)}"
        sends.append(f' send "{school}: {link}" to b')
    script = ('tell application "Messages"\n set svc to 1st service whose service type = iMessage\n'
              f' set b to buddy "{num}" of svc\n' + "\n".join(sends) + "\nend tell")
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=45)
        print(f"  texted {len(items)} link(s) -> {num}")
        return True
    except Exception as e:
        print("  text failed:", e); return False


def main():
    dry = "--dry" in sys.argv
    n_force = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else None
    slot_count = int(os.environ.get("SLOT_COUNT", "4"))   # carousels per slot (16/day = 4)
    for k, v in (("CRON_KEY", CRON_KEY), ("ANTHROPIC_KEY", ANTHROPIC_KEY)):
        if not v:
            print(f"{k} not set — refusing to run."); sys.exit(1)
    os.environ.setdefault("GRADER_FAST", "1")   # cheap Haiku grading for autopilot only
    start_local_server()
    try:
        if not wait_local_app():
            print("local render server did not start"); sys.exit(2)
        if "--slot" in sys.argv:
            # one slot: make SLOT_COUNT fresh carousels (options) and text all links.
            # retry each up to 3x — transient SQLite contention clears on retry.
            made = []
            for i in range(slot_count):
                for attempt in range(3):
                    try:
                        cid, name = make_one(dry=dry)
                        if cid:
                            made.append((cid, name)); break
                    except Exception as e:
                        print(f"  carousel {i} attempt {attempt} failed: {e}")
                        time.sleep(2)
            if made:
                text_carousels(made)
            print(f"slot done: {len(made)}/{slot_count} made")
            return
        if n_force is not None:
            need = n_force
        else:
            pending = buffer_pending()
            need = max(0, BUFFER_TARGET - pending)
            print(f"buffer: {pending} pending, target {BUFFER_TARGET} -> making {need}")
        ok = 0
        for i in range(need):
            try:
                cid, _ = make_one(dry=dry)
                if cid:
                    ok += 1
            except Exception as e:
                print(f"  carousel {i} failed: {e}")
        print(f"done: {ok}/{need}")
    finally:
        stop_local_server()


if __name__ == "__main__":
    main()
