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


# ── slide 1: title via gpt-image-1 ─────────────────────────────────────────
def gen_title_image(name, lines, accent_words):
    accent = ", ".join(f'"{w}"' for w in accent_words) or f'"{name}"'
    prompt = (
        "Vertical 9:16 social-media title slide, pure solid white background, no border, no frame. "
        "Typography only, in a heavy ultra-condensed bold uppercase sans-serif (like the font 'Anton'), "
        "center-aligned, lines stacked tightly with small gaps, EACH line scaled large so it spans almost "
        "the full width. Use EXACTLY these lines, one per line, spelled EXACTLY:\n"
        + "\n".join(lines) + "\n"
        f"Color these words in {name}'s official primary brand color: {accent}. Every other word is black. "
        f"Below the text, centered in the lower area, place the official {name} logo (recognizable, correct colors). "
        "Clean, crisp, high-contrast, modern. Correct spelling. No watermark, no extra words, no UI."
    )
    body = json.dumps({"model": "gpt-image-1", "prompt": prompt, "size": "1024x1536",
                       "quality": IMG_QUALITY, "n": 1}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/images/generations", data=body,
                                 headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    b64 = d["data"][0]["b64_json"]
    try:
        os.makedirs("/tmp/cren_factory", exist_ok=True)
        open("/tmp/cren_factory/title.png", "wb").write(base64.b64decode(b64))
    except Exception:
        pass
    return "data:image/png;base64," + b64


# ── slides 2 & 3: Candor exports via headless Chrome ───────────────────────
def _shot(out_path, url):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", "--window-size=1024,1536",
                    "--virtual-time-budget=6000", f"--screenshot={out_path}", url],
                   check=True, capture_output=True, timeout=120)
    with open(out_path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def render_candor_slides(slug, slide3):
    tmp = "/tmp/cren_factory"; os.makedirs(tmp, exist_ok=True)
    s2 = _shot(f"{tmp}/s2.png", f"{LOCAL_URL}/profile/{slug}/export?rkey={CRON_KEY}&clean=1")
    s3url = (f"{LOCAL_URL}/grade/export?rkey={CRON_KEY}&clean=1" if slide3 == "grade"
             else f"{LOCAL_URL}/chances/{slug}/export?rkey={CRON_KEY}&clean=1")
    s3 = _shot(f"{tmp}/s3.png", s3url)
    return s2, s3


def odds_grade_text(slug, profile):
    odds, grade, low, high, gnum = "", "", None, None, None
    try:
        merged = app.merged_school(app.COLLEGES_BY_SLUG[slug])
        fit, _ = app.compute_fit(profile, merged)
        low, high = app.estimate_odds(merged, fit, profile)
        odds = f"{low}–{high}% chance"
    except Exception as e:
        print("odds:", e)
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


def wait_local_app():
    for _ in range(45):
        try:
            if _get_json(f"{LOCAL_URL}/healthz").get("ok"):
                return True
        except Exception:
            time.sleep(1)
    return False


def make_one(dry=False):
    slug = random.choice(SCHOOLS)
    g = gen_profile.generate(slug)          # profile saved to 181 + title copy
    odds, grade, low, high, gnum = odds_grade_text(slug, g["profile"])
    short, accent = app._school_brand(slug, g["name"])
    # Titles never reveal a specific number — the grader/odds are non-deterministic
    # per render, so a number in the title could contradict slide 3. The real
    # value is shown on slide 3. (numeric_title kept for a future deterministic path.)
    lines, accent_words = g["lines"], g["accent_words"]
    s2, s3 = render_candor_slides(slug, g["slide3"])
    title_img = gen_title_image(g["name"], lines, accent_words)
    hook = " ".join(lines)
    payload = dict(school_slug=slug, school_name=g["name"], accent=accent, title_text=hook,
                   title_formula="llm", slide3_type=g["slide3"], profile_json=json.dumps(g["profile"]),
                   odds_text=odds, grade_text=grade, img1=title_img, img2=s2, img3=s3,
                   meta={"lines": lines, "accent_words": accent_words})
    if dry:
        print(f"  [dry] {slug} | {hook[:48]} | {g['slide3']} | {odds} {grade}")
        return True
    res = push(payload)
    print(f"  pushed #{res.get('id')} {slug} | {hook[:44]} | {odds} {grade} | pending={res.get('pending')}")
    return res.get("ok")


def main():
    dry = "--dry" in sys.argv
    n_force = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else None
    for k, v in (("CRON_KEY", CRON_KEY), ("ANTHROPIC_KEY", ANTHROPIC_KEY), ("OPENAI_KEY", OPENAI_KEY)):
        if not v:
            print(f"{k} not set — refusing to run."); sys.exit(1)
    env = dict(os.environ, PORT=str(LOCAL_PORT), CRON_KEY=CRON_KEY, ANTHROPIC_KEY=ANTHROPIC_KEY)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_local_app():
            print("local render app did not start"); sys.exit(2)
        if n_force is not None:
            need = n_force
        else:
            pending = buffer_pending()
            need = max(0, BUFFER_TARGET - pending)
            print(f"buffer: {pending} pending, target {BUFFER_TARGET} -> making {need}")
        ok = 0
        for i in range(need):
            try:
                if make_one(dry=dry):
                    ok += 1
            except Exception as e:
                print(f"  carousel {i} failed: {e}")
        print(f"done: {ok}/{need}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
