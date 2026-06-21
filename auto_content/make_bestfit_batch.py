#!/usr/bin/env python3
"""Render a batch of "best-fit" carousels to a folder for manual posting on a
SEPARATE test account — deliberately NOT pushed to the shared content_queue, so
it never touches the main autopilot rotation / connected accounts.

Each carousel's slides are saved as PNGs you can long-press/save and post by hand:
  ~/Desktop/candor_bestfit_tests/<timestamp>/NN_<variant>_<school>/slideN.png

Usage:
  python3 auto_content/make_bestfit_batch.py [--n 6] [--variant fit|dream|mix] [--slug umich]
Env: CRON_KEY required (renders against the in-process app, same as the factory).
"""
import os, sys, base64, time, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from auto_content import factory as F   # noqa: E402


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main():
    n = int(_arg("--n", "6"))
    variant = _arg("--variant", "mix")          # fit | dream | mix
    slug = _arg("--slug")                        # force one school (optional)
    if not os.environ.get("CRON_KEY"):
        print("CRON_KEY not set — refusing to run."); sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    outdir = os.path.expanduser(f"~/Desktop/candor_bestfit_tests/{stamp}")
    os.makedirs(outdir, exist_ok=True)

    # Capture each carousel's payload (dry mode normally discards it) and write PNGs.
    saved = {}
    orig = F._finish
    def cap(payload, dry, label):
        saved["p"] = payload
        return orig(payload, dry, label)
    F._finish = cap

    F.start_local_server()
    try:
        if not F.wait_local_app():
            print("local render server did not start"); sys.exit(2)
        made = 0
        for i in range(n):
            v = variant if variant in ("fit", "dream") else ("dream" if i % 2 else "fit")
            saved.clear()
            try:
                F.make_bestfit(dry=True, slug=slug, variant=v)   # dry=True => no queue push
            except Exception as e:
                print(f"  carousel {i} failed: {e}"); continue
            p = saved.get("p")
            if not p:
                continue
            school = (p.get("school_name") or "school").replace("Best Fit: ", "").strip().lower().replace(" ", "-")
            cdir = os.path.join(outdir, f"{made+1:02d}_{p.get('meta',{}).get('variant','fit')}_{school}")
            os.makedirs(cdir, exist_ok=True)
            slides = 0
            for k in ("img1", "img2", "img3", "img4"):
                d = p.get(k)
                if d and d.startswith("data:image"):
                    slides += 1
                    with open(os.path.join(cdir, f"slide{slides}.png"), "wb") as f:
                        f.write(base64.b64decode(d.split(",", 1)[1]))
            made += 1
            print(f"  [{made}/{n}] {p.get('title_text','')[:48]} -> {cdir} ({slides} slides)")
    finally:
        F.stop_local_server()

    print(f"\nDone: {made} best-fit carousel(s) in {outdir}")
    print("Open that folder, long-press to save the slides, and post to the test account.")
    # optional iMessage nudge if PHONE is set
    if os.environ.get("PHONE"):
        try:
            F.text_reminder(made, made)
        except Exception:
            pass


if __name__ == "__main__":
    main()
