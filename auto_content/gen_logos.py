#!/usr/bin/env python3
"""One-time: generate a crisp HD logo (1024px) for every school in INST_LOGOS via
gpt-image-1 and overwrite static/logos_inst/<slug>.png. Logos never change, so
this runs once and every (free, HTML) title reuses them forever. ~$2 total.

Env: OPENAI_KEY. Optional arg: comma-separated slugs to (re)do just those.
"""
import os, sys, json, base64, time, urllib.request, urllib.error, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

KEY = os.environ["OPENAI_KEY"]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "logos_inst")

slugs = sorted(set(app.INST_LOGOS) & set(app.COLLEGES_BY_SLUG))
if len(sys.argv) > 1:
    only = set(sys.argv[1].split(","))
    slugs = [s for s in slugs if s in only]


def gen(slug):
    name = app.COLLEGES_BY_SLUG[slug].get("name")
    prompt = (f"A single, instantly recognizable official logo for {name} — the university's primary "
              f"athletics or institutional mark — in {name}'s official brand colors, centered on a pure "
              f"solid white background, crisp clean flat vector style, high resolution, generous white "
              f"margin around it, no surrounding text or caption, no drop shadow.")
    body = json.dumps({"model": "gpt-image-1", "prompt": prompt, "size": "1024x1024",
                       "quality": "medium", "n": 1}).encode()
    for attempt in range(6):
        req = urllib.request.Request("https://api.openai.com/v1/images/generations", data=body,
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=180).read())
            open(os.path.join(OUT, f"{slug}.png"), "wb").write(base64.b64decode(d["data"][0]["b64_json"]))
            return f"OK {slug}"
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(15 * (attempt + 1))  # backoff on rate limit
                continue
            return f"ERR {slug} {e.code} {e.read().decode()[:100]}"
        except Exception as e:
            if attempt < 5:
                time.sleep(8); continue
            return f"ERR {slug} {e}"


print(f"generating {len(slugs)} logos -> {OUT}", flush=True)
ok = 0
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
    for r in ex.map(gen, slugs):
        print(" ", r, flush=True)
        ok += r.startswith("OK")
print(f"done: {ok}/{len(slugs)} ok")
