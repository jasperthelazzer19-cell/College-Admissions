"""Re-fetch INST_LOGOS at high resolution (2000px) from Wikimedia Commons (the
same Wikidata P154 source they came from), so the title-slide logos stay crisp
when scaled up. SAFE: backs up originals, fetches to a temp dir, and only swaps a
logo in when the new one matches the current one's aspect ratio (same logo, just
sharper) AND is higher-res. Free (Wikimedia, no API key)."""
import json, os, ssl, time, threading, urllib.request, urllib.parse, shutil, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import warnings; warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w"); import app; sys.stderr = sys.__stderr__

WIDTH = 2000
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "CandorLogoFetch/1.0 (https://candoradmit.com; contact jasper)"}
_lock = threading.Lock(); _last = [0.0]
def get(url):
    with _lock:
        dt = time.time() - _last[0]
        if dt < 0.34: time.sleep(0.34 - dt)
        _last[0] = time.time()
    for _ in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40, context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.5); continue
            raise
    raise RuntimeError("ratelimited")
def getj(u): return json.loads(get(u))
WP="https://en.wikipedia.org/w/api.php?"; WD="https://www.wikidata.org/w/api.php?"; CM="https://commons.wikimedia.org/w/api.php?"

# exact Wikipedia titles for accuracy (featured set); others fall back to COLLEGES name
from hires_logos import TITLES as FEATURED_TITLES
def title_for(slug):
    if slug in FEATURED_TITLES: return FEATURED_TITLES[slug]
    return (app.COLLEGES_BY_SLUG.get(slug) or {}).get("name") or slug

def qid_for(title):
    d = getj(WP + urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item",
        "redirects":"1","titles":title,"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]
    return (pp.get("pageprops") or {}).get("wikibase_item")
def logo_files(qid):
    # P154 logo, P158 seal, P94 coat of arms — the curated logos are often the
    # seal/shield, not the wordmark, so gather all and shape-match below.
    out = []
    for prop in ("P154", "P158", "P94"):
        try:
            d = getj(WD + urllib.parse.urlencode({"action":"wbgetclaims","entity":qid,"property":prop,"format":"json"}))
            for cl in d.get("claims",{}).get(prop,[]):
                try: out.append(cl["mainsnak"]["datavalue"]["value"])
                except Exception: pass
        except Exception: pass
    return list(dict.fromkeys(out))
def commons_png(filename, width):
    d = getj(CM + urllib.parse.urlencode({"action":"query","titles":"File:"+filename,
        "prop":"imageinfo","iiprop":"url|size|mime","iiurlwidth":str(width),"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]
    ii = (pp.get("imageinfo") or [{}])[0]
    return ii.get("thumburl") or ii.get("url")

DST = "static/logos_inst"; TMP = "/tmp/hires_new"; BAK = "static/logos_inst_bak"
os.makedirs(TMP, exist_ok=True)
if not os.path.isdir(BAK): shutil.copytree(DST, BAK); print("backed up ->", BAK)

def cur_aspect(slug):
    try:
        with Image.open(f"{DST}/{slug}.png") as im: return im.width / im.height, max(im.size)
    except Exception: return None, 0

def work(slug):
    try:
        qid = qid_for(title_for(slug))
        if not qid: return (slug, "no-qid")
        cur_ar, cur_px = cur_aspect(slug)
        best = None  # (ar_diff, tmp_path, px, fn)
        for i, fn in enumerate(logo_files(qid)):
            url = commons_png(fn, WIDTH)
            if not url: continue
            try:
                data = get(url)
            except Exception: continue
            if len(data) < 1500: continue
            path = f"{TMP}/{slug}__{i}.png"; open(path,"wb").write(data)
            try:
                with Image.open(path) as im: nw, nh = im.size
            except Exception: continue
            if not nw or not nh: continue
            new_ar = nw / nh
            ar_diff = abs(new_ar - cur_ar)/cur_ar if cur_ar else 0
            if cur_ar and ar_diff > 0.14: continue          # different logo variant — shape mismatch
            if max(nw, nh) <= cur_px + 50: continue          # not actually higher-res
            if best is None or ar_diff < best[0]:            # prefer the closest-shape match
                best = (ar_diff, path, max(nw, nh), fn)
        if best:
            shutil.copy(best[1], f"{TMP}/{slug}.png")
            return (slug, f"ok {best[2]}px (was {cur_px}) ar±{best[0]:.2f} {best[3][:28]}")
        return (slug, "no-match")
    except Exception as e:
        return (slug, "ERR "+str(e)[:60])

slugs = list(app.INST_LOGOS)
results = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for fut in as_completed([ex.submit(work, s) for s in slugs]):
        s, m = fut.result(); results[s] = m; print(f"  {s}: {m}")

upgraded = [s for s,m in results.items() if m.startswith("ok")]
print(f"\nUPGRADED {len(upgraded)}/{len(slugs)}")
print("NOT upgraded:", {s:m for s,m in results.items() if not m.startswith("ok")})
# swap the accepted ones in
for s in upgraded: shutil.copy(f"{TMP}/{s}.png", f"{DST}/{s}.png")
print("swapped in", len(upgraded), "hi-res logos")
