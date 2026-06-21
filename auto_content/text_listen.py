#!/usr/bin/env python3
"""Text-to-make: read incoming iMessages from the creator's number, and for any
that request a carousel ("make a Duke chances vid", "stanford grade", "UCLA glow
up"), generate that exact carousel and text the link back.

Polls the Messages DB (~/Library/Messages/chat.db) — the process running this
needs macOS Full Disk Access. Tracks the last-seen message so nothing repeats.
Run on a short launchd interval. Env: same as factory (CRON_KEY, ANTHROPIC_KEY, PHONE).
"""
import os, sys, json, sqlite3, subprocess, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from auto_content import factory, gen_profile

PHONE = os.environ.get("PHONE", "")
DIGITS = "".join(c for c in PHONE if c.isdigit())[-10:]   # match last 10 digits
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".text_state.json")
SCHOOLS = sorted(set(app.INST_LOGOS) & set(app.COLLEGES_BY_SLUG))


def _last_rowid():
    try:
        return json.load(open(STATE)).get("last_rowid", 0)
    except Exception:
        return 0


def _save_rowid(r):
    json.dump({"last_rowid": r}, open(STATE, "w"))


def new_messages():
    """Incoming texts from the creator's number since last check (ROWID, text)."""
    if not os.path.exists(CHAT_DB):
        print("chat.db not found"); return []
    last = _last_rowid()
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    # Don't filter on is_from_me — on a self-thread (Mac + phone share the Apple
    # ID) the user's own texts can show as is_from_me=1. We instead skip the bot's
    # OWN output by content (_is_bot_output) so we never loop on our replies.
    rows = con.execute(
        "SELECT m.ROWID, m.text FROM message m JOIN handle h ON m.handle_id=h.ROWID "
        "WHERE m.ROWID>? AND m.text IS NOT NULL AND h.id LIKE ? "
        "ORDER BY m.ROWID ASC", (last, f"%{DIGITS}%")).fetchall()
    con.close()
    return [(rid, t) for rid, t in rows if not _is_bot_output(t)]


def _is_bot_output(text):
    """True if this message is one the autopilot itself sent (so we never re-trigger
    on our own replies/notifications)."""
    t = (text or "").lower()
    markers = ("\U0001f3ac", "candoradmit.com", "/content/c/", "got it", "one sec",
               "carousel option", "long-press", "failed to generate")
    return any(m in t for m in markers)


def parse_request(text):
    """LLM-parse a text into {is_request, slug, slide3}. Cheap Haiku call."""
    catalog = ", ".join(f"{s}={app.COLLEGES_BY_SLUG[s].get('name')}" for s in SCHOOLS)
    prompt = (f'A user texted: "{text}"\nIs this asking to MAKE a Candor TikTok carousel for a school? '
              f'If yes, pick the matching school SLUG from this list and the type.\nSchools: {catalog}\n'
              f'Types: chances (odds), grade (out of 100), glowup (what it takes). Default chances.\n'
              f'Return ONLY JSON: {{"is_request": bool, "slug": "<slug or empty>", "slide3": "chances|grade|glowup"}}')
    try:
        m = gen_profile._claude().messages.create(model="claude-haiku-4-5-20251001", max_tokens=120,
                                                  messages=[{"role": "user", "content": prompt}])
        t = m.content[0].text.strip()
        if t.startswith("```"):
            t = t.split("```")[1].lstrip("json").strip()
        d = json.loads(t)
        return d if d.get("is_request") and d.get("slug") in SCHOOLS else None
    except Exception as e:
        print("parse error:", e); return None


def text_back(msg):
    if not PHONE:
        return
    script = ('tell application "Messages"\n set svc to 1st service whose service type = iMessage\n'
              f' set b to buddy "{PHONE}" of svc\n send "{factory._as_esc(msg)}" to b\nend tell')
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)


def _is_batch_trigger(text):
    """True if the text asks for a full batch (not a specific school)."""
    t = (text or "").lower().strip()
    if any(c.isalpha() for c in t) is False:   # e.g. just "5"
        return t in ("4", "5", "16", "20")
    keys = ("trigger", "make a batch", "batch", "run it", "run the", "fire the",
            "do a batch", "make 4", "make four", "make 5", "make five", "fresh batch", "give me a batch")
    return any(k in t for k in keys)


def _link(cid):
    return f"{factory.TARGET_URL}/content/c/{cid}?key={factory.urllib.parse.quote(factory.CRON_KEY)}"


def _web_batch():
    """Check the website '⚡ Make' buttons (/content/today). Returns (count, slugs)
    if a batch is pending — slugs is the creator's chosen schools (may be fewer
    than count; the rest auto-pick) — else None. Claims it immediately so a slow
    render doesn't double-fire on the next poll."""
    q = factory.urllib.parse.quote(factory.CRON_KEY)
    try:
        with urllib.request.urlopen(f"{factory.TARGET_URL}/content/batch-pending?key={q}", timeout=15) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        print("web batch poll:", e); return None
    if not d.get("pending"):
        return None
    try:
        claim = urllib.request.Request(
            f"{factory.TARGET_URL}/content/batch-claim?key={q}&id={d.get('id','')}", method="POST")
        urllib.request.urlopen(claim, timeout=15).read()
    except Exception as e:
        print("web batch claim:", e)
    try:
        n = max(1, min(8, int(d.get("count", 4))))
    except (TypeError, ValueError):
        n = 4
    slugs = [s for s in (d.get("slugs") or "").split(",") if s.strip()]
    return n, slugs, (d.get("kind") or "normal")


def main():
    msgs = new_messages()
    web = _web_batch()
    web_n = web[0] if web else None
    web_slugs = web[1] if web else []
    web_kind = web[2] if web else "normal"
    if not msgs and not web_n:
        return
    max_rowid = max((rid for rid, _ in msgs), default=_last_rowid())
    batch = bool(web_n) or any(_is_batch_trigger(t) for _, t in msgs)
    specifics = [(rid, r) for rid, t in msgs
                 for r in ([parse_request(t)] if not _is_batch_trigger(t) else [None]) if r]
    if not batch and not specifics:
        _save_rowid(max_rowid); return       # nothing actionable; advance cursor

    os.environ.setdefault("GRADER_FAST", "1")
    with factory.render_lock():   # serialize with the scheduled slot job (shared port + DB)
        _render_session(batch, web_n, web_slugs, specifics, max_rowid, web_kind)


def _render_session(batch, web_n, web_slugs, specifics, max_rowid, web_kind="normal"):
    factory.start_local_server()
    try:
        if not factory.wait_local_app():
            print("render server didn't start"); return
        if batch and web_kind == "bestfit":
            # Best-fit test angle (kept OFF the main wheel): render via make_bestfit;
            # slugs[0] holds the variant (fit/dream/mix). Never sets the skip-slot
            # marker — it doesn't stand in for a scheduled wheel slot.
            n = web_n or 6
            variant = web_slugs[0] if web_slugs else "mix"
            text_back(f"On it — making {n} best-fit carousels \U0001F3AF give me a few min...")
            made = []
            for i in range(n):
                v = variant if variant in ("fit", "dream") else ("dream" if i % 2 else "fit")
                for attempt in range(3):
                    try:
                        cid, name = factory.make_bestfit(variant=v)
                        if cid:
                            made.append((cid, name)); break
                    except Exception as e:
                        print(f"bestfit {i} attempt {attempt}: {e}")
            if made:
                factory.text_carousels(made)
            else:
                text_back("Hmm, that best-fit batch didn't generate — try again in a minute?")
            batch = False        # handled; fall through to any text specifics
        if batch:
            n = web_n if web_n else int(os.environ.get("SLOT_COUNT", "4"))
            text_back(f"On it — making a fresh batch of {n} \U0001F3AC give me a few min...")
            made = []
            for i in range(n):
                chosen = web_slugs[i] if i < len(web_slugs) else None  # creator-picked school or auto
                for attempt in range(3):
                    try:
                        cid, name = factory.make_one(slug=chosen, count_toward_cap=False)
                        if cid:
                            made.append((cid, name)); break
                    except Exception as e:
                        print(f"batch {i} attempt {attempt}: {e}")
            if made:
                factory.text_carousels(made)
                # The phone "Make N" button (a multi-carousel batch) stands in for
                # one scheduled slot — drop a marker so the next slot skips itself,
                # to avoid double-posting. BUT only during the active posting day
                # (08:00–19:30 local): a batch made overnight (after the last 7:30
                # slot or before the 8am slot) should NOT cancel the next scheduled
                # slot — there's nothing to double up with, and skipping would just
                # lose the morning's content. "Make 1" (n==1) never skips.
                import datetime as _dt
                _now = _dt.datetime.now().time()
                _active_day = _dt.time(8, 0) <= _now < _dt.time(19, 30)
                if web_n and web_n > 1 and _active_day:
                    try:
                        open(os.path.expanduser("~/.candor_skip_next_slot"), "w").close()
                        print(f"set skip-next-slot marker (web batch n={web_n})")
                    except Exception as e:
                        print("skip marker write failed:", e)
                elif web_n and web_n > 1:
                    print(f"overnight batch (n={web_n}) — NOT skipping next slot; the next scheduled slot still fires")
            else:
                # never go silent on a button/text batch — surface the failure
                text_back("Hmm, that batch didn't generate — try again in a minute?")
        for rid, r in specifics:
            try:
                text_back(f"Got it — making your {app.COLLEGES_BY_SLUG[r['slug']].get('name')} "
                          f"{r['slide3']} carousel, one sec...")
                cid, name = factory.make_one(slug=r["slug"], slide3=r["slide3"])
                if cid:
                    text_back(f"Done \U0001F3AC {name}: {_link(cid)}")
            except Exception as e:
                print(f"make failed for {r}: {e}")
                text_back("Sorry, that one failed to generate — try again?")
        _save_rowid(max_rowid)
    finally:
        factory.stop_local_server()


if __name__ == "__main__":
    if not (factory.CRON_KEY and factory.ANTHROPIC_KEY and PHONE):
        print("missing env"); sys.exit(1)
    main()
