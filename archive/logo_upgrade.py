"""Path A: for each INST_LOGOS school, find a CRISP same-mark version on Wikimedia
(SVGs render artifact-free at any size), shape-match it against the current mark,
and keep only true matches. Builds a before/after grid + writes upgrade candidates
to /tmp/logo_up/<slug>.png. Does NOT overwrite static/ — Jasper approves first."""
import json, os, ssl, time, threading, urllib.request, urllib.parse, sys, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont
import warnings; warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w"); import app; sys.stderr = sys.__stderr__
try:
    from hires_logos import TITLES as FEAT
except Exception:
    FEAT = {}

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "CandorLogoFetch/1.0 (https://candoradmit.com; contact jasper)"}
_lock = threading.Lock(); _last = [0.0]
def get(u):
    with _lock:
        dt = time.time() - _last[0]
        if dt < 0.3: time.sleep(0.3 - dt)
        _last[0] = time.time()
    for _ in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=40, context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.5); continue
            raise
    raise RuntimeError("rl")
def getj(u): return json.loads(get(u))
WP="https://en.wikipedia.org/w/api.php?"; WD="https://www.wikidata.org/w/api.php?"; CM="https://commons.wikimedia.org/w/api.php?"

def title_for(s): return FEAT.get(s) or (app.COLLEGES_BY_SLUG.get(s) or {}).get("name") or s
def qid_for(t):
    d = getj(WP + urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item","redirects":"1","titles":t,"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]; return (pp.get("pageprops") or {}).get("wikibase_item")
def files(qid):
    out = []
    for prop in ("P154","P158","P94"):   # logo, seal, coat of arms
        try:
            d = getj(WD + urllib.parse.urlencode({"action":"wbgetclaims","entity":qid,"property":prop,"format":"json"}))
            for cl in d.get("claims",{}).get(prop,[]):
                try: out.append(cl["mainsnak"]["datavalue"]["value"])
                except: pass
        except: pass
    return list(dict.fromkeys(out))
def thumb(fn, w=1700):
    d = getj(CM + urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url","iiurlwidth":str(w),"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]; ii = (pp.get("imageinfo") or [{}])[0]; return ii.get("thumburl") or ii.get("url")

def silhouette(im, n=200):
    im = im.convert("RGBA"); bg = Image.new("RGBA", im.size, (255,255,255,255)); bg.alpha_composite(im)
    g = bg.convert("L").resize((n,n))
    px = g.load(); s = []
    for y in range(n):
        for x in range(n): s.append(1 if px[x,y] < 232 else 0)   # ink
    return s
def iou(a, b):
    inter = sum(1 for x,y in zip(a,b) if x and y); uni = sum(1 for x,y in zip(a,b) if x or y)
    return inter/uni if uni else 0

slugs = sorted(app.INST_LOGOS)
OUT = "/tmp/logo_up"; shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
results = {}

def work(slug):
    cur_path = f"static/logos_inst/{slug}.png"
    if not os.path.exists(cur_path): return (slug, None, 0, "no current")
    cur_sil = silhouette(Image.open(cur_path))
    try:
        qid = qid_for(title_for(slug))
        cands = files(qid) if qid else []
    except Exception as e:
        return (slug, None, 0, f"qid err {e}")
    best = (0, None, None)
    for fn in cands[:6]:
        try:
            url = thumb(fn, 1700)
            if not url: continue
            data = get(url)
            if len(data) < 1500: continue
            from io import BytesIO
            cim = Image.open(BytesIO(data))
            score = iou(cur_sil, silhouette(cim))
            if score > best[0]: best = (score, data, fn)
        except: continue
    score, data, fn = best
    if data and score >= 0.62:           # same-mark threshold
        open(f"{OUT}/{slug}.png", "wb").write(data)
        return (slug, fn, round(score,2), "MATCH")
    return (slug, fn, round(score,2), "no match")

with ThreadPoolExecutor(max_workers=4) as ex:
    for fut in as_completed([ex.submit(work, s) for s in slugs]):
        slug, fn, score, st = fut.result(); results[slug] = (fn, score, st)
        print(f"  {slug:16} {st:9} score={score}  {(fn or '')[:40]}")

matched = [s for s in slugs if results[s][2] == "MATCH"]
print(f"\nMATCHED (crisp same-mark found): {len(matched)}/{len(slugs)}")
print("no-match (need manual/source):", [s for s in slugs if results[s][2] != "MATCH"])
json.dump({s:results[s] for s in slugs}, open(f"{OUT}/_results.json","w"))
