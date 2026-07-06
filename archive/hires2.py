"""Fetch sharp logos from Wikimedia for all 42, matching the graphic's variant.
Most of the graphic uses ATHLETIC marks, so pull from the athletics article's
logo (P154); use the institutional article for wordmark schools; hard-code
direct Commons files for the ones flagged wrong. SVG sources render at 1024px
so they're always crisp. Saves to /tmp/hi2/<slug>.png + comparison sheet."""
import json, os, ssl, time, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "CandorLogoFetch/1.0 (https://candoradmit.com; jasper)"}
_lock = threading.Lock(); _last = [0.0]
def get(url):
    with _lock:
        dt = time.time() - _last[0]
        if dt < 0.34: time.sleep(0.34 - dt)
        _last[0] = time.time()
    for _ in range(4):
        try: return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.5); continue
            raise
    raise RuntimeError("rl")
def getj(u): return json.loads(get(u))
WP="https://en.wikipedia.org/w/api.php?"; WD="https://www.wikidata.org/w/api.php?"; CM="https://commons.wikimedia.org/w/api.php?"

# article to read P154 from (athletics article where the graphic uses an athletic mark)
ART = {
 "harvard":"Harvard Crimson","yale":"Yale Bulldogs","princeton":"Princeton Tigers",
 "columbia":"Columbia Lions","upenn":"Penn Quakers","brown":"Brown Bears",
 "dartmouth":"Dartmouth Big Green","cornell":"Cornell Big Red","stanford":"Stanford Cardinal",
 "ucla":"UCLA Bruins","ucb":"California Golden Bears","uva":"Virginia Cavaliers",
 "unc":"North Carolina Tar Heels","usc":"USC Trojans","nyu":"NYU Violets",
 "northwestern":"Northwestern Wildcats","umich":"Michigan Wolverines","wisc":"Wisconsin Badgers",
 "tulane":"Tulane Green Wave","duke":"Duke Blue Devils","jhu":"Johns Hopkins Blue Jays",
 "notre-dame":"Notre Dame Fighting Irish","vanderbilt":"Vanderbilt Commodores",
 "rice":"Rice Owls","gatech":"Georgia Tech Yellow Jackets","georgetown":"Georgetown Hoyas",
 "wake-forest":"Wake Forest Demon Deacons","bu":"Boston University Terriers",
 "villanova":"Villanova Wildcats","ut-austin":"Texas Longhorns","uf":"Florida Gators",
 "northeastern":"Northeastern Huskies","miami":"Miami Hurricanes","bc":"Boston College Eagles",
 "ucsd":"UC San Diego Tritons","vanderbilt":"Vanderbilt Commodores",
 # institutional wordmark/crest schools -> university article
 "mit":"Massachusetts Institute of Technology","cmu":"Carnegie Mellon University",
 "uchicago":"Chicago Maroons","washu":"Washington University in St. Louis",
 "emory":"Emory University","tufts":"Tufts University",
}
# direct Commons file overrides for ones that need a specific variant
OVERRIDE = {
 "caltech":"Caltech Logo.svg",
 "stanford":"Stanford Cardinal logo.svg",
 "mit":"MIT logo.svg",
 "nyu":"New York University logo.svg",
}

def qid(title):
    d=getj(WP+urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item","redirects":"1","titles":title,"format":"json"}))
    p=d["query"]["pages"]; pp=p[list(p)[0]]; return (pp.get("pageprops") or {}).get("wikibase_item")
def p154(q):
    d=getj(WD+urllib.parse.urlencode({"action":"wbgetclaims","entity":q,"property":"P154","format":"json"}))
    return [c["mainsnak"]["datavalue"]["value"] for c in d.get("claims",{}).get("P154",[]) if c["mainsnak"].get("datavalue")]
def png(fn,w=1024):
    d=getj(CM+urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url|mime","iiurlwidth":str(w),"format":"json"}))
    p=d["query"]["pages"]; pp=p[list(p)[0]]; ii=(pp.get("imageinfo") or [{}])[0]; return ii.get("thumburl") or ii.get("url")

os.makedirs("/tmp/hi2",exist_ok=True)
def work(slug):
    try:
        files=[]
        if slug in OVERRIDE: files.append(OVERRIDE[slug])
        if slug in ART:
            q=qid(ART[slug])
            if q: files += p154(q)
        for fn in files:
            u=png(fn,1024)
            if not u: continue
            data=get(u)
            if len(data)<1200: continue
            open(f"/tmp/hi2/{slug}.png","wb").write(data)
            return (slug,f"ok {len(data)//1024}KB {fn[:34]}")
        return (slug,"none")
    except Exception as e: return (slug,"ERR "+str(e)[:40])

res={}
with ThreadPoolExecutor(max_workers=3) as ex:
    for f in as_completed([ex.submit(work,s) for s in (set(ART)|set(OVERRIDE))]):
        s,m=f.result(); res[s]=m
for s in sorted(res): print(s,res[s])

# comparison: graphic vs hi2
order=sorted(set(ART)|set(OVERRIDE))
PW=150;CELL=PW*2+10;H=170;cols=4;rows=(len(order)+cols-1)//cols
sh=Image.new("RGB",(cols*CELL,rows*H),(248,248,248));d=ImageDraw.Draw(sh)
def place(src,x,y,tag):
    d.rectangle([x,y,x+PW-1,y+H-1],outline=(205,205,205))
    if src and os.path.exists(src):
        try:
            im=Image.open(src).convert("RGBA");bg=Image.new("RGBA",im.size,(255,255,255,255));bg.alpha_composite(im);im=bg.convert("RGBA")
            im.thumbnail((PW-14,H-26));b=Image.new("RGBA",(PW-2,H-2),(255,255,255,255));b.alpha_composite(im,((PW-2-im.width)//2,(H-2-im.height)//2-5));sh.paste(b.convert("RGB"),(x+1,y+1))
        except: d.text((x+5,y+H//2),"ERR",fill=(255,0,0))
    else: d.text((x+5,y+H//2),"—",fill=(160,160,160))
    d.text((x+3,y+H-12),tag,fill=(15,15,15))
for i,slug in enumerate(order):
    cx=(i%cols)*CELL;cy=(i//cols)*H
    place(f"/tmp/crops/{slug}.png",cx,cy,slug+" GFX")
    place(f"/tmp/hi2/{slug}.png",cx+PW+10,cy,"NEW")
sh.save("/tmp/hi2_compare.png");print("sheet -> /tmp/hi2_compare.png")
