"""Fetch HIGH-RES official university logos from Wikimedia Commons (Wikidata P154
logo property) for the 42 featured schools. Saves to /tmp/hires/<slug>.png at
~1024px, then builds a comparison sheet vs the current extracted crops so we can
verify quality + that the variant matches before swapping."""
import json, os, ssl, time, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw

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
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.5); continue
            raise
    raise RuntimeError("ratelimited")
def getj(u): return json.loads(get(u))

WP = "https://en.wikipedia.org/w/api.php?"
WD = "https://www.wikidata.org/w/api.php?"
CM = "https://commons.wikimedia.org/w/api.php?"

# slug -> exact Wikipedia article title (for accurate Wikidata lookup)
TITLES = {
 "harvard":"Harvard University","yale":"Yale University","princeton":"Princeton University",
 "columbia":"Columbia University","upenn":"University of Pennsylvania","brown":"Brown University",
 "dartmouth":"Dartmouth College","cornell":"Cornell University",
 "mit":"Massachusetts Institute of Technology","stanford":"Stanford University",
 "ucla":"University of California, Los Angeles","ucb":"University of California, Berkeley",
 "uva":"University of Virginia","unc":"University of North Carolina at Chapel Hill",
 "usc":"University of Southern California","nyu":"New York University",
 "caltech":"California Institute of Technology","northwestern":"Northwestern University",
 "umich":"University of Michigan","wisc":"University of Wisconsin–Madison",
 "tulane":"Tulane University","duke":"Duke University","jhu":"Johns Hopkins University",
 "uchicago":"University of Chicago","notre-dame":"University of Notre Dame",
 "vanderbilt":"Vanderbilt University","washu":"Washington University in St. Louis",
 "rice":"Rice University","cmu":"Carnegie Mellon University","emory":"Emory University",
 "gatech":"Georgia Institute of Technology","georgetown":"Georgetown University",
 "tufts":"Tufts University","wake-forest":"Wake Forest University","bu":"Boston University",
 "villanova":"Villanova University","ut-austin":"University of Texas at Austin",
 "uf":"University of Florida","northeastern":"Northeastern University",
 "miami":"University of Miami","bc":"Boston College","ucsd":"University of California, San Diego",
}

def qid_for(title):
    d = getj(WP + urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item",
        "redirects":"1","titles":title,"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]
    return (pp.get("pageprops") or {}).get("wikibase_item")

def logo_files(qid):
    # P154 = logo image. Return all candidate filenames.
    d = getj(WD + urllib.parse.urlencode({"action":"wbgetclaims","entity":qid,"property":"P154","format":"json"}))
    out = []
    for cl in d.get("claims", {}).get("P154", []):
        try: out.append(cl["mainsnak"]["datavalue"]["value"])
        except Exception: pass
    return out

def commons_png(filename, width=1024):
    d = getj(CM + urllib.parse.urlencode({"action":"query","titles":"File:"+filename,
        "prop":"imageinfo","iiprop":"url|size|mime","iiurlwidth":str(width),"format":"json"}))
    p = d["query"]["pages"]; pp = p[list(p)[0]]
    ii = (pp.get("imageinfo") or [{}])[0]
    return ii.get("thumburl") or ii.get("url"), ii.get("mime","")

os.makedirs("/tmp/hires", exist_ok=True)
def work(slug):
    try:
        qid = qid_for(TITLES[slug])
        if not qid: return (slug, "no-qid")
        for fn in logo_files(qid):
            url, mime = commons_png(fn, 1024)
            if not url: continue
            data = get(url)
            if len(data) < 1500: continue
            with open(f"/tmp/hires/{slug}.png", "wb") as f: f.write(data)
            return (slug, f"ok {len(data)//1024}KB {fn[:40]}")
        return (slug, "no-logo-file")
    except Exception as e:
        return (slug, "ERR "+str(e)[:50])

results = {}
with ThreadPoolExecutor(max_workers=3) as ex:
    for fut in as_completed([ex.submit(work, s) for s in TITLES]):
        slug, msg = fut.result(); results[slug] = msg

ok = [s for s,m in results.items() if m.startswith("ok")]
bad = {s:m for s,m in results.items() if not m.startswith("ok")}
print(f"HIRES OK: {len(ok)}/{len(TITLES)}")
print("FAILED:", bad)

# comparison sheet: hi-res (top) vs current extracted crop (bottom) per school
order = sorted(TITLES)
cols = 7; cw = 200; rows = len(order)
sheet = Image.new("RGB", (cols*cw, 0))  # placeholder
H = 230
sheet = Image.new("RGB", (7*cw, ((len(order)+6)//7)*H), (255,255,255))
d = ImageDraw.Draw(sheet)
for i, slug in enumerate(order):
    x = (i % 7) * cw; y = (i // 7) * H
    d.rectangle([x,y,x+cw-1,y+H-1], outline=(200,200,200))
    p = f"/tmp/hires/{slug}.png"
    if os.path.exists(p):
        try:
            im = Image.open(p).convert("RGBA")
            bg = Image.new("RGBA", im.size, (255,255,255,255)); bg.alpha_composite(im); im = bg.convert("RGBA")
            im.thumbnail((cw-30, H-50))
            base = Image.new("RGBA",(cw-4,H-4),(255,255,255,255))
            base.alpha_composite(im, ((cw-4-im.width)//2,(H-4-im.height)//2-6))
            sheet.paste(base.convert("RGB"),(x+2,y+2))
        except Exception: d.text((x+8,y+H//2),"ERR",fill=(255,0,0))
    else:
        d.text((x+8,y+H//2), results.get(slug,"?")[:18], fill=(200,0,0))
    d.text((x+6,y+H-16), slug, fill=(10,10,10))
sheet.save("/tmp/hires_sheet.png")
print("comparison sheet -> /tmp/hires_sheet.png")
