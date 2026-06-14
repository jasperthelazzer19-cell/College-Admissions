"""Fetch REAL institutional logos from Wikidata's logo property (P154) for the
profile slides. Wikipedia lead images are mostly campus photos, so we go:
  school name -> Wikipedia pageprops -> Wikidata Qid -> P154 logo file ->
  Commons imageinfo PNG thumbnail -> download to static/logos_inst/<slug>.png.
Profile slides prefer these; the athletic logos (landing/chances) are untouched.
"""
import json, os, re, ssl, sys, time, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings; warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w"); import app; sys.stderr = sys.__stderr__

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "CandorLogoFetch/1.0 (https://candoradmit.com; contact jasper)"}
_lock = threading.Lock(); _last = [0.0]
def get(url):
    # global throttle to stay under Wikimedia's rate limit (avoid 429)
    with _lock:
        dt = time.time() - _last[0]
        if dt < 0.12: time.sleep(0.12 - dt)
        _last[0] = time.time()
    for _ in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25, context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.0); continue
            raise
    raise RuntimeError("ratelimited")
def getj(url): return json.loads(get(url))

WP = "https://en.wikipedia.org/w/api.php?"
WD = "https://www.wikidata.org/w/api.php?"
CM = "https://commons.wikimedia.org/w/api.php?"

def qid_for(title):
    q = {"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
         "redirects": "1", "titles": title, "format": "json"}
    d = getj(WP + urllib.parse.urlencode(q))
    pages = d["query"]["pages"]; p = pages[list(pages)[0]]
    return (p.get("pageprops") or {}).get("wikibase_item")

def logo_file(qid):
    q = {"action": "wbgetclaims", "entity": qid, "property": "P154", "format": "json"}
    d = getj(WD + urllib.parse.urlencode(q))
    cl = d.get("claims", {}).get("P154") or []
    if not cl: return None
    return cl[0]["mainsnak"]["datavalue"]["value"]

def commons_png(filename):
    q = {"action": "query", "titles": "File:" + filename, "prop": "imageinfo",
         "iiprop": "url", "iiurlwidth": "600", "format": "json"}
    d = getj(CM + urllib.parse.urlencode(q))
    pages = d["query"]["pages"]; p = pages[list(pages)[0]]
    ii = (p.get("imageinfo") or [{}])[0]
    return ii.get("thumburl") or ii.get("url")

os.makedirs("static/logos_inst", exist_ok=True)
def work(c):
    slug, name = c["slug"], c["name"]
    cands = [name]
    if not re.search(r"(university|college|institute|school)$", name, re.I):
        cands += [name + " University", name + " College"]
    for t in cands:
        try:
            qid = qid_for(t)
            if not qid: continue
            fn = logo_file(qid)
            if not fn: continue
            url = commons_png(fn)
            if not url: continue
            data = get(url)
            if len(data) < 600: continue
            with open(f"static/logos_inst/{slug}.png", "wb") as f: f.write(data)
            return (slug, name)
        except Exception:
            continue
    return None

cols = app.COLLEGES
got = []
with ThreadPoolExecutor(max_workers=5) as ex:
    futs = {ex.submit(work, c): c for c in cols}
    done = 0
    for f in as_completed(futs):
        r = f.result(); done += 1
        if r: got.append(r)
        if done % 50 == 0: print(f"  {done}/{len(cols)} ... {len(got)} logos")

print(f"\nGOT {len(got)}/{len(cols)} institutional logos")
mp = {slug: f"/static/logos_inst/{slug}.png" for slug, _ in got}
json.dump(mp, open("inst_logos_map.json", "w"))
print("got slugs:", sorted(s for s, _ in got))
