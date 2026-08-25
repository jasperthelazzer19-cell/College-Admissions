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
import os, sys, time, json, base64, random, subprocess, tempfile, urllib.request, urllib.error, urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CRON_KEY      = os.environ.get("CRON_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
OPENAI_KEY    = os.environ.get("OPENAI_KEY", "")
TARGET_URL    = os.environ.get("TARGET_URL", "https://admit.up.railway.app").rstrip("/")
BUFFER_TARGET = int(os.environ.get("BUFFER_TARGET", "6"))   # one per account (6 accounts) — clean set, no flood
LOCAL_PORT    = int(os.environ.get("LOCAL_PORT", "5077"))
LOCAL_URL     = f"http://127.0.0.1:{LOCAL_PORT}"
CHROME        = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
IMG_QUALITY   = os.environ.get("IMG_QUALITY", "medium")
# Blank-render guard (see _shot). Real slides run 200KB–1MB; a raced/blank capture
# lands at ~25KB, which is how empty title slides reached the queue.
_SHOT_MIN_BYTES = int(os.environ.get("SLIDE_MIN_BYTES", "100000"))
_SHOT_BUDGET_MS = int(os.environ.get("SHOT_BUDGET_MS", "15000"))
_SHOT_TRIES     = int(os.environ.get("SHOT_TRIES", "3"))

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
def _shot(out_path, url, verify=True, static=False):
    # Guard against screenshotting an error/redirect page (e.g. an export whose
    # profile is missing 302-redirects to the editor, or an h2h "student not set"
    # notice) and silently posting it. Fetch first: if it redirected to /profile
    # or the body lacks a render card, fail so the carousel is skipped/retried.
    html = None
    if verify or static:
        try:
            # 180s: the reveal routes (chances/grade/glowup) warm-fetch runs the
            # bullets/grade LLM once via the Max CLI, which can take >90s. A short
            # timeout here was silently failing chances slides (the staple format).
            resp = urllib.request.urlopen(url, timeout=180)
            final = resp.geturl()
            html = resp.read().decode("utf-8", "ignore")
        except Exception as e:
            raise RuntimeError(f"render fetch failed ({e}): {url}")
        if final.rstrip("/").endswith("/profile"):
            raise RuntimeError(f"export redirected to profile editor (missing data): {url}")
        if 'id="card"' not in html and 'id="tcard"' not in html:
            raise RuntimeError(f"render page missing a card (likely an error/notice page): {url}")
    target = url
    if static and html:
        # Screenshot the ALREADY-RENDERED HTML (file://) instead of re-hitting the
        # route — the reveal routes run slow LLM work (bullets + counterfactual
        # grades) per request, and on the Max-CLI path that outlasts the capture
        # timeout, producing a BLACK slide. The fetch above ran the route once; we
        # now capture that exact HTML statically (no LLM during the screenshot).
        # Rewrite root-relative asset URLs (logos at /static/...) to absolute so
        # file:// loads them from the local server.
        html = (html.replace('="/static', f'="{LOCAL_URL}/static')
                    .replace("='/static", f"='{LOCAL_URL}/static"))
        tmpf = out_path + ".html"
        with open(tmpf, "w", encoding="utf-8") as f:
            f.write(html)
        target = "file://" + tmpf
    # Extra flags for containerized Chromium (root needs --no-sandbox, small
    # /dev/shm needs --disable-dev-shm-usage). No-op on the Mac (unset).
    extra = os.environ.get("CHROME_EXTRA_FLAGS", "").split()
    # Chrome exits 0 even when it hits --timeout, writing a blank/partial frame.
    # Under parallel workers that raced constantly and shipped empty slides to the
    # queue (blank title, no logo). A real slide is >200KB; a blank one lands at
    # ~25KB — so treat an undersized PNG as a failed render and retry with a bigger
    # paint budget. Raising (not returning) means the carousel is skipped, never
    # pushed blank.
    last = 0
    for attempt in range(_SHOT_TRIES):
        budget = _SHOT_BUDGET_MS * (attempt + 1)
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=2", "--window-size=1024,1536",
                        f"--virtual-time-budget={budget}", f"--timeout={budget * 2}", *extra,
                        f"--screenshot={out_path}", target],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        last = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if last >= _SHOT_MIN_BYTES:
            with open(out_path, "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode()
        print(f"  blank render ({last:,}b < {_SHOT_MIN_BYTES:,}), retry {attempt + 1}/{_SHOT_TRIES}: {url}",
              flush=True)
    raise RuntimeError(f"blank render after {_SHOT_TRIES} tries ({last:,}b): {url}")


def render_slides(slug, slide3, lines, stats_cover=False, uid=181):
    """All 3 slides as PNG data URLs, rendered FREE via headless Chrome against the
    local Candor app. Slide 1 = HTML title (per-line fill + HD cached logo),
    slides 2 & 3 = the Candor profile + chances/grade exports. stats_cover renders
    slide 1 as the 'Can I get into X?' + stats-on-cover layout instead.
    uid = which demo profile the slides show (parallel workers use their own
    uid so concurrent generations can't overwrite each other's profile)."""
    tmp = tempfile.mkdtemp(prefix="cren_")
    hook = urllib.parse.quote("\n".join(lines))
    _stats = "&stats=1" if stats_cover else ""
    s1 = _shot(f"{tmp}/s1.png", f"{LOCAL_URL}/title/export?rkey={CRON_KEY}&clean=1&slug={slug}&hook={hook}{_stats}")
    s2 = _shot(f"{tmp}/s2.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid={uid}")
    s3routes = {"grade": f"/grade/export?uid={uid}", "glowup": f"/glowup/{slug}/export?uid={uid}",
                "chances": f"/chances/{slug}/export?uid={uid}"}
    s3path = s3routes.get(slide3, s3routes["chances"])
    # verify=True warm-fetches the reveal route FIRST (60s timeout), which runs the
    # bullets LLM once and memoizes it (app._bullet_memo). The screenshot then hits
    # the cached route and renders instantly — fixes the black slide-3 that happened
    # when a slow (Max-CLI) bullets gen outlasted the screenshot's 12s timeout.
    # Generation is free now, so the old "don't double-fire the LLM" reason is moot.
    s3 = _shot(f"{tmp}/s3.png", f"{LOCAL_URL}{s3path}&rkey={CRON_KEY}&clean=1", static=True)
    return s1, s2, s3


def odds_grade_text(slug, profile, want_grade=True, uid=181):
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
            g = app._grade_cached(uid, profile, compute=True)
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
import fcntl, contextlib  # noqa: E402
_server = None

# Keep the lock in /tmp, NOT the Desktop project dir — the Desktop folder's
# cloud-sync/file-provider raised EDEADLK ("Resource deadlock avoided") on the
# bare open(), crashing scheduled slots. /tmp is local, no sync, no TCC.
_LOCKFILE = os.path.join(tempfile.gettempdir(), "candor_render.lock")


@contextlib.contextmanager
def render_lock(timeout=900):
    """Cross-process exclusive lock. The scheduled slot job and the text/button
    listener both render against ONE local server (port 5077) and the SAME shared
    user 181/38 profile rows — running concurrently would (a) collide on the port
    and (b) let one carousel's profile slide pair with another's odds slide. Hold
    this around the whole generate→render session so the two jobs serialize.

    Acquire NON-BLOCKING + retry instead of a blocking LOCK_EX: on macOS a blocking
    flock can spuriously raise EDEADLK ("Resource deadlock avoided") when the other
    job already holds it, which would crash the run instead of just waiting (this is
    what killed the 11:55 one-shot). Polling LOCK_NB waits cleanly up to `timeout`."""
    f = None
    acquired = False
    deadline = time.time() + timeout
    while True:
        try:
            if f is None:
                f = open(_LOCKFILE, "w")          # open inside the retry too — it can EDEADLK
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if f is not None:
                try: f.close()
                except Exception: pass
                f = None
            if time.time() > deadline:
                raise RuntimeError("render_lock: timed out waiting for the other render job")
            time.sleep(2)
    try:
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
        f.close()


def start_local_server():
    """Serve the Candor app IN THIS PROCESS (a daemon thread) so the generator and
    the render endpoints share ONE set of DB connections — no cross-process SQLite
    'disk I/O error'. Replaces spawning a separate app.py.

    WAIT for the port instead of dying on it. make_server raises OSError when the
    port is taken and callers invoke this outside a try, so an hourly autopilot slot
    still holding 5077 took down the whole 6:40am hot-take batch. Renders can't just
    borrow the neighbour's server either: it shuts down when that slot finishes and
    every in-flight screenshot dies with 'Connection refused' mid-run. Slots are
    short, so waiting for a clean bind of our own is both simpler and safer."""
    global _server
    from werkzeug.serving import make_server
    last = None
    for _ in range(60):          # ~2 min; an autopilot slot is far shorter than this
        try:
            _server = make_server("127.0.0.1", LOCAL_PORT, app.app, threaded=True)
            threading.Thread(target=_server.serve_forever, daemon=True).start()
            return
        except OSError as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"port {LOCAL_PORT} still busy after 2 min: {last}")


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


def _title_png(slug, lines, accent_words, nologo=False, bg=None, fg=None):
    hook = urllib.parse.quote("\n".join(mark_accents(lines, accent_words)))
    extra = "&nologo=1" if nologo else ""
    if bg: extra += "&bg=" + urllib.parse.quote(bg)
    if fg: extra += "&fg=" + urllib.parse.quote(fg)
    return _shot(os.path.join(tempfile.mkdtemp(prefix="cren_t_"), "t.png"),
                 f"{LOCAL_URL}/title/export?rkey={CRON_KEY}&clean=1&slug={slug}&hook={hook}{extra}")


# cid -> assigned account label, filled at push time so the texted links can
# tell the creator which account to post each carousel to (same-process batch).
_assigned_account = {}


# ── hook A/B test: which arm this carousel is being rendered for ───────────
# The arm has to be decided HERE, before slide 1 is rendered, because the title
# is burned into a PNG — by the time Railway picks an account the hook is already
# pixels. So we choose the arm, render its hook, and send `hook_arm` with the
# push; app._assign_content_account then only considers accounts in that arm.
# Strict alternation (not a coin flip) so a 2-week run can't drift into 60/40 by
# luck and cost us the comparison. Counter lives in .post_state.json alongside
# the school counters and is guarded by the same lock, since parallel workers
# (PARALLEL_GEN) call this concurrently.
# Arm definition itself lives in app.HOOK_AB_ARMS — the single place to edit.
def _next_hook_arm():
    if not getattr(app, "HOOK_AB_ACTIVE", False):
        return None
    with _state_lock:
        d = _post_state()
        n = int(d.get("ab_n", 0)) + 1
        d["ab_n"] = n
        try:
            json.dump(d, open(_POST_STATE, "w"))
        except Exception:
            pass
    # (_post_state resets at midnight, so the counter restarts each day — with
    # 16 carousels/day that still lands 8/8, and it never accumulates skew.)
    return "A" if n % 2 else "B"


def _finish(payload, dry, label):
    if dry:
        print(f"  [dry] {label}")
        return None, payload["school_name"]
    res = push(payload)
    cid = res.get("id") if res.get("ok") else None
    if cid:
        _assigned_account[cid] = res.get("assigned_account")
    _arm = payload.get("hook_arm")     # hook A/B — printed so a run log shows the split
    print(f"  pushed #{res.get('id')} {label} | -> {res.get('assigned_account')}"
          f"{' [arm ' + _arm + ']' if _arm else ''} | pending={res.get('pending')}")
    return cid, payload["school_name"]


def make_single(dry=False, slug=None, slide3=None, uid=181):
    slug = slug if slug in SCHOOLS else (_pick_school(respect_cap=False) or random.choice(SCHOOLS))
    # Pick the reveal type up front (unless text-to-make requested one) and steer
    # the hook to match — chances stays the staple, grade + glow-up get real
    # airtime so the feed cycles every format instead of defaulting to chances.
    if slide3 not in ("chances", "grade", "glowup"):
        # Base weights re-set 2026-07-26 from the FIRST trustworthy attribution data
        # (622 posts; the loop had been running on 2 data points because the retention
        # purge was orphaning links). Measured avg views / comments-per-post:
        #   chances 380 / 0.159   glowup 370 / 0.183   grade 330 / 0.132
        # glow-up nearly matches chances on reach and beats everything on comments —
        # the signal TikTok actually pushes on — while it was the LEAST produced
        # format. grade is the weakest of the three. _blend still adjusts from here;
        # this just stops it starting from a prior the data contradicts.
        w = _blend({"chances": 5, "grade": 2, "glowup": 4}, _tt_perf())
        keys = list(w)
        slide3 = random.choices(keys, weights=[w[k] for k in keys])[0]
    arm = _next_hook_arm()                                        # hook A/B (None when off)
    g = gen_profile.generate(slug, uid=uid, want_slide3=slide3, arm=arm)   # profile -> worker uid + matching hook
    g["slide3"] = slide3
    odds, grade, low, high, gnum = odds_grade_text(slug, g["profile"],
                                                   want_grade=(g["slide3"] == "grade"), uid=uid)
    short, accent = app._school_brand(slug, g["name"])
    lines, accent_words = g["lines"], g["accent_words"]
    title_formula = "llm"
    # Result-forward variant (~30%): put the REAL verdict on the cover — but ONLY
    # for chances. Chances odds are deterministic (estimate_odds), so the cover
    # number always matches the reveal slide. Grade is LLM-graded and recomputed
    # at slide render, so a numeric grade on the cover could disagree with the
    # slide (the "74/100 vs 85" bug). Grade carousels keep a teaser hook instead.
    # Arm A is excluded from the result-forward variant on purpose: a cover that
    # reads "CANDOR GAVE THIS APPLICANT A 12-18% CHANCE" IS the odds-lookup hook
    # we're testing against, so letting it through would put ~30% of the control
    # hook into the treatment arm and blur the result. Arm B keeps it unchanged.
    if arm != "A" and g["slide3"] == "chances" and random.random() < float(os.environ.get("RESULT_FORWARD_RATE", "0.3")):
        nt = numeric_title(short, g["slide3"], low, high, gnum)
        if nt:
            lines, accent_words = nt
            title_formula = "numeric"
    marked = mark_accents(lines, accent_words)
    s1, s2, s3 = render_slides(slug, g["slide3"], marked, uid=uid)
    hook = " ".join(lines)
    payload = dict(school_slug=slug, school_name=g["name"], accent=accent, title_text=hook,
                   title_formula=title_formula, slide3_type=g["slide3"], profile_json=json.dumps(g["profile"]),
                   odds_text=odds, grade_text=grade, img1=s1, img2=s2, img3=s3,
                   hook_arm=arm,
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
    slug = _pick_school(respect_cap=False) or random.choice(SCHOOLS)  # weighted anchor; compare is exempt from the daily cap
    # Compare's cover is a fixed layout ("THIS STUDENT APPLIED TO A, B & C") with no
    # written hook, so there's no title to vary — but it still needs an arm so it
    # lands on the right accounts and gets the right CAPTION. Leaving it armless
    # would let control captions leak onto arm-A accounts.
    arm = _next_hook_arm()
    comp = [slug] + _band_of(slug)          # 3 schools, similar selectivity
    g = gen_profile.generate(slug)          # profile -> 181
    odds, _, _, _, _ = odds_grade_text(slug, g["profile"], want_grade=False)
    accent = app._school_brand(slug, g["name"])[1]
    tmp = tempfile.mkdtemp(prefix="cren_")
    s1 = _shot(f"{tmp}/c1.png", f"{LOCAL_URL}/title-compare/export?rkey={CRON_KEY}&clean=1&slugs={','.join(comp)}")
    s2 = _shot(f"{tmp}/c2.png", f"{LOCAL_URL}/profile-neutral/export?rkey={CRON_KEY}&clean=1")
    # static=True: pre-fetch the HTML (90s timeout) and screenshot the static
    # file — the live-URL capture blanked whenever the route outlasted Chrome's
    # ~6s virtual-time budget (the July blank-compare-slide bug).
    s3 = _shot(f"{tmp}/c3.png", f"{LOCAL_URL}/compare/export?rkey={CRON_KEY}&clean=1&slugs={','.join(comp)}", static=True)
    shorts = ", ".join(app._school_brand(s, app.COLLEGES_BY_SLUG[s].get('name'))[0] for s in comp)
    payload = dict(school_slug=slug, school_name=f"Compare: {shorts}", accent=accent,
                   title_text=f"THIS STUDENT APPLIED TO {shorts}", title_formula="compare",
                   slide3_type="compare", profile_json=json.dumps(g["profile"]),
                   odds_text=odds, grade_text="", img1=s1, img2=s2, img3=s3,
                   hook_arm=arm, meta={"compare": comp})
    return _finish(payload, dry, f"COMPARE {shorts}")


def make_h2h(dry=False, slug=None):
    """Head-to-head: 4 slides — title, Student A profile, Student B profile, and
    the A-vs-B result for one school. Generates TWO profiles (181=A, 38=B)."""
    slug = slug if slug in SCHOOLS else (_pick_school(respect_cap=False) or random.choice(SCHOOLS))
    arm = _next_hook_arm()                     # hook A/B (None when off)
    gA = gen_profile.generate(slug, uid=181)   # student A
    gen_profile.generate(slug, uid=38)         # student B
    short, accent = app._school_brand(slug, gA["name"])
    lines = ["WHICH STUDENT", "GETS INTO", f"{short}?"]
    if arm == "A":
        # h2h is the heaviest-weighted format (weight 6 vs single 5), so leaving its
        # one hard-coded title alone would hand arm A a control hook a third of the
        # time. Same new mechanics as _TITLE_FAMILIES_A: commit the viewer before
        # the reveal, or tell them the room is split. None of these claim an
        # outcome — we don't know which student wins until the reveal slide.
        lines = random.choice([
            ["DON'T SCROLL.", "PICK A OR B", "FOR", f"{short}"],
            ["ONE OF THESE", "GETS INTO", f"{short}.", "THE COMMENTS", "ARE SPLIT"],
            ["PICK ONE.", "ONLY ONE", "GETS INTO", f"{short}"],
            ["THE OBVIOUS PICK", "HERE IS", "USUALLY", "THE WRONG ONE"],
            ["MOST PEOPLE", "READ THIS", "MATCHUP", "BACKWARDS"],
        ])
    tmp = tempfile.mkdtemp(prefix="cren_")
    s1 = _title_png(slug, lines, [short])
    s2 = _shot(f"{tmp}/h2.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=181&label=STUDENT%20A")
    s3 = _shot(f"{tmp}/h3.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1&uid=38&label=STUDENT%20B")
    s4 = _shot(f"{tmp}/h4.png", f"{LOCAL_URL}/headtohead/{slug}/export?rkey={CRON_KEY}&clean=1", static=True)
    payload = dict(school_slug=slug, school_name=f"H2H: {gA['name']}", accent=accent,
                   title_text=" ".join(lines), title_formula="h2h", slide3_type="h2h",
                   profile_json=json.dumps(gA["profile"]), odds_text="", grade_text="",
                   img1=s1, img2=s2, img3=s3, img4=s4, hook_arm=arm,
                   meta={"head_to_head": True})
    return _finish(payload, dry, f"H2H {short}")


def _pick_bestfit_seed():
    """Pick a vibe-rich SEED school (sports/Greek-strong per app's own data),
    weighted by real demand + iconic floors, avoiding recent picks. Its real
    attributes become the student's preferences; the engine then ranks the winner."""
    import candor_fit
    pool = candor_fit.vibe_pool(SCHOOLS, app)
    recent = set(_post_state().get("recent", [])[-RECENT_NOREPEAT:])
    cand = [s for s in pool if s not in recent] or pool
    dem = _demand()
    base = {s: max(dem.get(s, 0) + SCHOOL_WEIGHT_BASELINE, _floor(s)) ** DEMAND_GAMMA for s in cand}
    return random.choices(list(base), weights=list(base.values()))[0]


def make_bestfit(dry=False, slug=None, variant=None):
    """'Where should they go?' — lifestyle/fit angle, powered by Candor's REAL My
    Fit engine. Pick a vibe-rich seed school → set its real attributes as the
    student's preferences → app.compute_my_fit ranks EVERY school → the #1 is the
    reveal (Candor's honest My Fit pick). Traits = the prefs the student set.

    variant 'fit'  (3 slides): hook → trait list → 'BEST FIT: <#1>' + logo.
    variant 'dream'(4 slides): 'DREAM SCHOOL: <reach>' + logo → trait list →
            'BUT CANDOR THINKS THEIR BEST FIT IS...' → '<#1>' + logo."""
    import candor_fit
    seed = slug if slug in SCHOOLS else _pick_bestfit_seed()
    seed_merged = app.merged_school(app.COLLEGES_BY_SLUG[seed])
    prefs = candor_fit.prefs_for_school(seed_merged, app)
    # Pick the reveal winner the SAME WAY the /bestfit/result page does (same pool,
    # same base_profile, same compute_my_fit) so the cover's school / color / hashtags
    # can NEVER disagree with the slide-2 reveal. The old code ranked with a different
    # function + a California state, so the cover said one school (UNC-blue text,
    # #unc hashtags) while the reveal page showed another (Maryland).
    profile = candor_fit.base_profile(major=prefs.get("major"))
    profile.update(prefs)
    pool = sorted(set(app.INST_LOGOS) & set(app.COLLEGES_BY_SLUG))
    scored = []
    for s in pool:
        try:
            score, _ = app.compute_my_fit(profile, app.merged_school(app.COLLEGES_BY_SLUG[s]))
            scored.append((s, score))
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    slug = scored[0][0] if scored else seed       # the engine's #1 = the reveal
    _push_recent(slug)
    sch = app.COLLEGES_BY_SLUG[slug]
    short, accent = app._school_brand(slug, sch.get("name"))
    tmp = tempfile.mkdtemp(prefix="cren_")
    pq = urllib.parse.urlencode(prefs)        # prefs -> /bestfit/result reveal page
    # win=slug pins the reveal to exactly the school we used for the cover, so they're
    # always the same school even if the prefs round-trip drifts.
    reveal_url = f"{LOCAL_URL}/bestfit/result?rkey={CRON_KEY}&clean=1&win={slug}&{pq}"

    # Cover wants = traits the student asked for that the REVEAL school actually has,
    # so the cover never promises something the reveal contradicts (e.g. "BIG SPORTS"
    # over a moderate-sports school). Falls back to the winner's own defining traits.
    win_prefs = candor_fit.prefs_for_school(app.merged_school(sch), app)
    shared = {k: v for k, v in prefs.items() if win_prefs.get(k) == v}
    # Show 3-4 wants (Jasper: title was too vague at 2). title_wants pulls every
    # want the prefs justify, randomizes the phrasing + order, so the categories
    # vary across generations instead of always reading "BIG SPORTS / GREEK LIFE".
    # Fall back to the winner's own full traits if the shared set is too thin to
    # hit 3 — still the reveal school's REAL attributes, so cover never lies.
    wants = candor_fit.title_wants(shared, n=4)
    if len(wants) < 3:
        wants = candor_fit.title_wants(win_prefs, n=4)
    title_lines = wants + ["WHAT COLLEGE?"]   # bare hook: 3-4 wants + the question
    # text in the best-fit school's brand color on a clean white slide
    s1 = _title_png(slug, title_lines, [], nologo=True, fg=accent)
    s2 = _shot(f"{tmp}/bf_reveal.png", reveal_url, verify=False)
    payload = dict(school_slug=slug, school_name=f"Best Fit: {short}", accent=accent,
                   title_text=", ".join(w.lower() for w in wants) + " — what college?",
                   title_formula="bestfit", slide3_type="bestfit",
                   profile_json="{}", odds_text="", grade_text="",
                   img1=s1, img2=s2, meta={"bestfit": True, "wants": wants})
    return _finish(payload, dry, f"BESTFIT {short} <- {', '.join(wants)}")


# ── daily head-to-head cap (they cost ~2-4x a normal carousel) ─────────────
# Raised 5 -> 8 on 2026-08-25. The cap was written when generation cost money and
# when h2h was merely "second best"; on the Aug 12-25 data it is the clear top
# format (869 avg vs compare's 345, 9 of 37 posts over 1k) and generation is $0
# on the Max CLI, so the cap was spending the best format's slots on the worst.
# Cost is now wall-clock only: /headtohead/*/export runs ~83s with the claim
# verifier. Drop this back to 5 if the buffer starts draining faster than it fills.
H2H_DAILY_CAP = int(os.environ.get("H2H_DAILY_CAP", "8"))
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


# ── TikTok performance feedback ────────────────────────────────────────────
# Blend the static format weights with how each format ACTUALLY performs on
# TikTok (engagement score over carousels we attributed to a real post). A format
# needs at least TT_PERF_MIN_N attributed posts before it sways its own weight,
# the adjustment is scaled by TT_PERF_ALPHA, and every weight is clamped to
# [lo,hi]× its base so one viral fluke can't starve the rotation. Set
# TT_FEEDBACK=0 to turn the whole thing off and fall back to the static weights.
#
# RETUNED 2026-07-26, first time this ran on real data. Until the hashtag
# attribution fix there were 2 attributed posts, so `good` never reached 2 keys
# and _blend was a pass-through — every "learned" weight was in fact the static
# base. On the 633 recovered posts the old constants moved ctype weights by at
# most 4.1% and slide3 by 5.2%: a feedback loop that read as learning and wasn't.
#
# MIN_N 3 -> 6. Per-post score sd is 311 against a mean of 388, so at n=3 the
# standard error of a group's mean is 180 (46% of the mean) while the true
# spread BETWEEN schools is only 68 (17.5%). n=3 is ~80% noise; n=6 is the point
# where the estimate carries as much signal as the shrinkage prior can use.
#
# ALPHA 0.6 -> 1.0. app.tiktok_perf_by already returns an empirical-Bayes
# posterior (shrunk by K=12 effective posts, the measured within/between
# variance ratio), so the right thing to do with it is act on it exactly.
# alpha=0.6 was a second, arbitrary discount ON TOP of a correctly shrunk
# estimate — double-conservatism that pinned the loop near the static base.
# Net effect: a format with 200 posts now moves ~1.6x harder than before, a
# school with 5 posts moves LESS. That is the whole point — learn where there's
# evidence, stop guessing where there isn't.
TT_PERF_MIN_N = int(os.environ.get("TT_PERF_MIN_N", "6"))
TT_PERF_ALPHA = float(os.environ.get("TT_PERF_ALPHA", "1.0"))


def _tt_perf(group="slide3_type"):
    """{key: (n, avg_engagement_score)} from attributed posts, grouped by
    slide3_type (format) or school_slug. {} when disabled or unavailable, so the
    caller falls back to its static weights."""
    if os.environ.get("TT_FEEDBACK") == "0":
        return {}
    try:
        return app.tiktok_perf_by(group) or {}
    except Exception:
        return {}


def _blend(base, perf, lo=0.4, hi=2.0):
    """base: {key: weight}. perf: {key: (n, shrunk engagement score)}. Returns a
    new weight dict nudged toward higher-performing keys. Keys with <TT_PERF_MIN_N
    samples keep their base weight. No-op (returns base) unless at least two keys
    have enough data to compare.

    The [lo,hi] clamp is catastrophe insurance, not the throttle: measured on the
    633 attributed posts at ALPHA=1.0 the widest multiplier anywhere is 0.89-1.60
    (schools) and 0.79-1.15 (formats), so it never binds. If it ever does bind,
    something upstream is wrong — check for a mis-attributed viral before raising
    it."""
    good = {k: v for k, (n, v) in perf.items() if k in base and n >= TT_PERF_MIN_N}
    if len(good) < 2:
        return dict(base)
    mean = sum(good.values()) / len(good)
    if mean <= 0:
        return dict(base)
    out = {}
    for k, w in base.items():
        if k in good:
            mult = max(lo, min(hi, 1 + TT_PERF_ALPHA * (good[k] / mean - 1)))
            out[k] = w * mult
        else:
            out[k] = w
    return out


# ── school selection: weight by real user demand + cap repeats per day ─────
SCHOOL_DAILY_CAP = int(os.environ.get("SCHOOL_DAILY_CAP", "2"))  # max same-school per day (single/grade/glowup/h2h; compare excluded)
SCHOOL_WEIGHT_BASELINE = 2          # so low/no-demand schools still get some airtime
# Demand curve steepness: weight = (demand+baseline or floor) ** DEMAND_GAMMA.
# 1.0 = linear (old). >1 = most-calculated schools dominate harder. 1.4 = moderate.
# LEFT AT 1.4 ON PURPOSE, but know what it is and isn't buying: on the 633
# attributed posts, corr(log 90-day calc demand, TikTok engagement score) is
# -0.078 across the 28 schools with n>=8. Demand does NOT predict TikTok reach.
# It's a CONVERSION lever (post the schools users actually look up, so the click
# lands on a page they want), not a reach lever. Changing gamma is an owner call
# about which of those two the feed is for.
DEMAND_GAMMA = float(os.environ.get("DEMAND_GAMMA", "1.4"))
ICONIC_FLOOR = 45                   # iconic/household-name schools get AT LEAST this
# selection weight regardless of calc volume — so BC (4 calcs), Notre Dame (7),
# Georgetown etc. still post regularly, since they pop on TikTok by name alone.
# (Only renderable slugs — must exist in INST_LOGOS/COLLEGES or it's a no-op.)
ICONIC_SCHOOLS = {
    "harvard","yale","princeton","columbia","upenn","brown","dartmouth","cornell",
    "stanford","mit","caltech","duke","northwestern","uchicago","jhu","vanderbilt",
    "rice","washu","emory","notre-dame","georgetown","bc","tufts","nyu","usc","cmu","bu",
    "ucla","ucb","umich","uva","unc","gatech","ut-austin","ucsd","wisc","uf",
    "williams","amherst",
    # added 2026-06-16 (recommended batch 1)
    "northeastern","miami","wake-forest","tulane","villanova",
    # big publics that already had logos (instant-add)
    "uw","uiuc","umd","purdue","rutgers",
}
# MEGA = the household names that pop hardest on TikTok. They get a much higher
# floor than plain iconic schools so they show up roughly 2-3x as often.
MEGA_SCHOOLS = {
    "harvard","stanford","mit","yale","princeton","columbia","upenn","brown",
    "cornell","dartmouth","umich","nyu","usc","ucla","ucb","duke","northwestern","uchicago",
}
MEGA_FLOOR = int(os.environ.get("MEGA_FLOOR", "110"))   # vs ICONIC_FLOOR 45
# 110 -> 150 (Jul 2026) -> back to 110 (2026-07-26). The bump was made off a
# "992-post audit" claim that big names dominate the 600+ view band. That audit
# could not have used attribution — only 2 posts were attributed at the time —
# and the 633 posts the hashtag fix recovered say the opposite:
#   MEGA posts            366 avg engagement score (N=380)
#   non-MEGA posts        422                      (N=253)   diff -56, t=-1.91
#   iconic-but-not-MEGA   430                      (N=201)   vs MEGA t=+1.79
# At 150 the 18 MEGA schools held 63% of all selection probability while
# performing ~13% BELOW the second tier. 110 walks that back to the last setting
# that wasn't justified by bad data rather than inventing a new number. Set
# MEGA_FLOOR=150 to restore if the feed reads too obscure.
# Don't repeat a school within the last N picks (across ALL accounts) so the feed
# stays varied. Applies to the headline/anchor school of every carousel.
RECENT_NOREPEAT = int(os.environ.get("RECENT_NOREPEAT", "6"))
_POST_STATE = os.path.join(HERE, ".post_state.json")
_demand_cache = None


def _demand():
    """Per-school calc volume from the live site (cached per process) so we post
    schools roughly in proportion to what users actually look up. {} -> uniform."""
    global _demand_cache
    if _demand_cache is None:
        try:
            _demand_cache = _get_json(f"{TARGET_URL}/content/school-demand?key={urllib.parse.quote(CRON_KEY)}") or {}
        except Exception as e:
            print("demand fetch failed (uniform fallback):", e)
            _demand_cache = {}
    return _demand_cache


def _post_state():
    import datetime
    today = datetime.date.today().isoformat()
    try:
        d = json.load(open(_POST_STATE))
    except Exception:
        d = {}
    if d.get("date") != today:
        d = {"date": today, "counts": {}}
    return d


def _bump_school(slug):
    with _state_lock:
        return _bump_school_unlocked(slug)

def _bump_school_unlocked(slug):
    d = _post_state()
    d["counts"][slug] = d["counts"].get(slug, 0) + 1
    try:
        json.dump(d, open(_POST_STATE, "w"))
    except Exception:
        pass


def _push_recent(slug):
    with _state_lock:
        return _push_recent_unlocked(slug)

def _push_recent_unlocked(slug):
    """Record the just-picked school so the next RECENT_NOREPEAT picks avoid it."""
    d = _post_state()
    rec = [s for s in d.get("recent", []) if s != slug]
    rec.append(slug)
    d["recent"] = rec[-(RECENT_NOREPEAT * 3):]
    try:
        json.dump(d, open(_POST_STATE, "w"))
    except Exception:
        pass


def _floor(s):
    if s in MEGA_SCHOOLS:
        return MEGA_FLOOR
    if s in ICONIC_SCHOOLS:
        return ICONIC_FLOOR
    return 0


def _pick_school(respect_cap=True, avoid_recent=True):
    """Weighted pick: P(school) ∝ real demand + baseline, with mega/iconic floors.
    Excludes any school in the last RECENT_NOREPEAT picks (feed variety) and, with
    respect_cap, any school already at SCHOOL_DAILY_CAP today. Records its pick."""
    st = _post_state()
    counts = st.get("counts", {})
    recent = set(st.get("recent", [])[-RECENT_NOREPEAT:]) if avoid_recent else set()

    def _pool(use_recent):
        return [s for s in SCHOOLS
                if (not respect_cap or counts.get(s, 0) < SCHOOL_DAILY_CAP)
                and (s not in recent if use_recent else True)]
    pool = _pool(True) or _pool(False)   # relax the no-repeat if it empties the pool
    if not pool:
        return None
    dem = _demand()
    # demand+baseline, but mega/iconic schools never fall below their floor so the
    # household names stay in heavy rotation regardless of raw calc volume.
    base = {s: max(dem.get(s, 0) + SCHOOL_WEIGHT_BASELINE, _floor(s)) ** DEMAND_GAMMA for s in pool}
    # Nudge toward schools whose carousels actually perform on TikTok (clamped so a
    # viral school gets more airtime without starving the long tail).
    weighted = _blend(base, _tt_perf("school_slug"))
    keys = list(weighted)
    pick = random.choices(keys, weights=[weighted[k] for k in keys])[0]
    _push_recent(pick)
    return pick


def resolve_format(fmt):
    """Map a creator-picked format name (from the Today 'Make' button) to make_one's
    (ctype, slide3) args. Returns (None, None) for auto/normal so make_one rotates."""
    return {
        "chances": ("single", "chances"),
        "grade":   ("single", "grade"),
        "glowup":  ("single", "glowup"),
        "compare": ("compare", None),
        "h2h":     ("h2h", None),
        "single":  ("single", None),
    }.get((fmt or "").strip().lower(), (None, None))


_serial_lock = threading.Lock()     # compare/h2h/bestfit share fixed demo uids (181/38)
_state_lock = threading.Lock()      # guards .post_state.json read-modify-write

def make_one(dry=False, slug=None, slide3=None, ctype=None, count_toward_cap=True, uid=181):
    """Dispatcher. Explicit slug (text-to-make) -> single, uncapped. Otherwise
    rotate formats; for the per-school formats (single/h2h) pick the school
    weighted by real demand and never post the same school more than
    SCHOOL_DAILY_CAP times/day (compare is excluded from that cap).

    count_toward_cap=False decouples this carousel from the SCHEDULED daily cap:
    it neither respects nor increments the per-day counts. Used by the on-demand
    'Make N' button so a manual batch (e.g. late at night) never eats into — or
    gets suppressed by — the scheduled 8/12/3/6/9 slots."""
    explicit = bool(slug)
    if ctype is None:
        if slug:
            ctype = "single"
        else:
            # ctype perf: "single" pools chances/grade/glowup; compare/h2h map 1:1.
            perf = _tt_perf()
            single = [(n, v) for st, (n, v) in perf.items() if st in ("chances", "grade", "glowup")]
            cperf = {}
            if single:
                tot = sum(n for n, _ in single)
                cperf["single"] = (tot, sum(n * v for n, v in single) / max(1, tot))
            for st in ("compare", "h2h"):
                if st in perf:
                    cperf[st] = perf[st]
            # Re-weighted 2026-08-25 on 237 attributed posts from Aug 12-25, the
            # first clean read since the rotation consolidated to four accounts:
            #   h2h      n=37  avg 869  9 posts >=1k  (best ever recorded: 4,952)
            #   single   n=100 avg ~500 6 posts >=1k
            #   compare  n=46  avg 345  ZERO posts >=1k, best 718
            # compare is the format that has never once broken out across two
            # separate audits (cut 3->2 in July on the same verdict); h2h is 2.5x
            # its average and owns the tail. compare 2 -> 1, h2h 6 -> 8.
            w = _blend({"single": 6, "compare": 1, "h2h": 8}, cperf)   # lean into "which student gets in" (h2h)
            keys = list(w)
            ctype = random.choices(keys, weights=[w[k] for k in keys])[0]
            if ctype == "h2h" and count_toward_cap and _h2h_today() >= H2H_DAILY_CAP:
                # Overflow used to be random.choice(["single","compare"]) — a coin
                # flip that sent half of the best format's spillover into the worst
                # one, ignoring the weights entirely. Fall back proportionally.
                _fb = {k: w[k] for k in ("single", "compare") if w.get(k, 0) > 0} or {"single": 1}
                ctype = random.choices(list(_fb), weights=list(_fb.values()))[0]
    if ctype == "compare":
        with _serial_lock:
            return make_compare(dry)
    # NOTE: 'bestfit' is intentionally NOT in the auto rotation — it's a separate
    # test angle for a dedicated account. Reachable only via explicit ctype
    # (make_bestfit_batch.py renders them straight to a folder, no queue push).
    if ctype == "bestfit":          # lifestyle/fit angle — picks its own school
        with _serial_lock:
            return make_bestfit(dry)
    # single / h2h target ONE school — weight by demand, respect the daily cap
    # (scheduled posts only; manual batches pass count_toward_cap=False)
    if not explicit:
        slug = _pick_school(respect_cap=count_toward_cap)
        if slug is None:           # everything hit the cap today -> do a compare
            return make_compare(dry)
    if ctype == "h2h":
        if count_toward_cap:
            _bump_h2h()
        with _serial_lock:
            res = make_h2h(dry, slug=slug)
    else:
        res = make_single(dry, slug, slide3, uid=uid)
    if not explicit and res and res[0] and count_toward_cap:   # count scheduled auto-posts toward the cap
        _bump_school(slug)
    return res


def _as_esc(s):
    """Escape a string for embedding inside an AppleScript double-quoted literal.
    School names like St. John's / Saint Mary's contain quotes that would
    otherwise break the osascript and drop the text silently."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def text_carousels(items):
    """iMessage the creator one intro + a tappable link per carousel (file
    attachments are broken via AppleScript on macOS; links send fine).
    items = list of (cid, school_name)."""
    num = os.environ.get("PHONE", "")
    if not num or not items:
        print("PHONE not set or nothing to text — skipping"); return False
    intro = (f"\U0001F3AC {len(items)} new Candor carousel option(s) — tap each, "
             f"long-press the slides to save:")
    sends = [f' send "{_as_esc(intro)}" to b']
    for cid, school in items:
        link = f"{TARGET_URL}/content/c/{cid}?key={urllib.parse.quote(CRON_KEY)}"
        acct = _assigned_account.get(cid)
        # show "@user" as-is, a bare number as "acct N"
        who = (f" → {acct}" if (acct and (acct.startswith('@') or not acct.isdigit()))
               else (f" → acct {acct}" if acct else ""))
        sends.append(f' send "{_as_esc(f"{school}{who}: {link}")}" to b')
    script = ('tell application "Messages"\n set svc to 1st service whose service type = iMessage\n'
              f' set b to buddy "{num}" of svc\n' + "\n".join(sends) + "\nend tell")
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=45)
        print(f"  texted {len(items)} link(s) -> {num}")
        return True
    except Exception as e:
        print("  text failed:", e); return False


def text_reminder(made, pending):
    """iMessage a plain 'slides are ready' nudge (no links) after topping up the
    queue — the creator opens /content/today to post from the buffer."""
    num = os.environ.get("PHONE", "")
    if not num:
        print("PHONE not set — skipping reminder"); return False
    msg = f"✅ {made} fresh Candor slides are done — {pending} ready to post in the queue."
    script = ('tell application "Messages"\n set svc to 1st service whose service type = iMessage\n'
              f' set b to buddy "{num}" of svc\n send "{_as_esc(msg)}" to b\nend tell')
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=45)
        print(f"  texted reminder -> {num}")
        return True
    except Exception as e:
        print("  reminder text failed:", e); return False


def main():
    dry = "--dry" in sys.argv
    n_force = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else None
    slot_count = int(os.environ.get("SLOT_COUNT", "4"))   # carousels per slot (16/day = 4)
    for k, v in (("CRON_KEY", CRON_KEY), ("ANTHROPIC_KEY", ANTHROPIC_KEY)):
        if not v:
            print(f"{k} not set — refusing to run."); sys.exit(1)
    os.environ.setdefault("GRADER_FAST", "1")   # cheap Haiku grading for autopilot only
    with render_lock():   # serialize with the text/button listener (shared port + DB)
        _run_session(dry, n_force, slot_count)


def _run_session(dry, n_force, slot_count):
    start_local_server()
    try:
        if not wait_local_app():
            print("local render server did not start"); sys.exit(2)
        if "--slot" in sys.argv:
            # If the creator hit the phone "Make 4" button since the last slot, that
            # full manual batch stands in for one scheduled batch — skip this slot
            # once (consume the marker). The "Make 1" button does NOT set this.
            skip_marker = os.path.expanduser("~/.candor_skip_next_slot")
            if not dry and os.path.exists(skip_marker):
                os.remove(skip_marker)
                print("slot skipped: consumed manual Make-4 batch marker")
                return
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
        # PARALLEL_GEN>1: generate with a worker pool. Each worker owns a demo
        # uid so concurrent generations never overwrite each other's profile
        # mid-render; compare/h2h/bestfit serialize internally (_serial_lock).
        workers = max(1, min(6, int(os.environ.get("PARALLEL_GEN", "1"))))
        uid_pool = [181, 9101, 9102, 9103, 9104, 9105][:workers]
        ok = 0
        if workers == 1:
            for i in range(need):
                try:
                    cid, _ = make_one(dry=dry)
                    if cid:
                        ok += 1
                except Exception as e:
                    print(f"  carousel {i} failed: {e}")
        else:
            def _task(i):
                try:
                    cid, _ = make_one(dry=dry, uid=uid_pool[i % workers])
                    return 1 if cid else 0
                except Exception as e:
                    print(f"  carousel {i} failed: {e}")
                    return 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                ok = sum(ex.map(_task, range(need)))
        print(f"done: {ok}/{need}")
        # Buffer top-up runs quietly now — the creator asked to stop the hourly
        # "slides are ready" nudge. A separate per-slot "time to post" text
        # (com.candor.postreminder, fired at 8/12/3/6/9) handles the nudging.
    finally:
        stop_local_server()


if __name__ == "__main__":
    main()
