"""Path A, take 2: athletic marks live on the ATHLETICS-PROGRAM Wikidata entity
(e.g. 'Wake Forest Demon Deacons'), not the university entity (which has the
wordmark). Gather candidates from BOTH + the seal/coat-of-arms, shape-match each
against the CURRENT mark, keep only true same-mark matches at crisp resolution.
Writes upgrades to /tmp/logo_up2/<slug>.png + a before/after grid. No overwrite."""
import json, os, ssl, time, threading, urllib.request, urllib.parse, sys, shutil
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont
import warnings; warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w"); import app; sys.stderr = sys.__stderr__

# athletics-program article titles (where the athletic marks live)
TEAM = {
 "harvard":"Harvard Crimson","yale":"Yale Bulldogs","princeton":"Princeton Tigers","cornell":"Cornell Big Red",
 "brown":"Brown Bears","dartmouth":"Dartmouth Big Green","columbia":"Columbia Lions","upenn":"Penn Quakers",
 "mit":"MIT Engineers","caltech":"Caltech Beavers","stanford":"Stanford Cardinal","duke":"Duke Blue Devils",
 "northwestern":"Northwestern Wildcats","notre-dame":"Notre Dame Fighting Irish","vanderbilt":"Vanderbilt Commodores",
 "rice":"Rice Owls","wake-forest":"Wake Forest Demon Deacons","tulane":"Tulane Green Wave","miami":"Miami Hurricanes",
 "bc":"Boston College Eagles","bu":"Boston University Terriers","georgetown":"Georgetown Hoyas","nyu":"NYU Violets",
 "usc":"USC Trojans","ucla":"UCLA Bruins","ucb":"California Golden Bears","ucsd":"UC San Diego Tritons",
 "umich":"Michigan Wolverines","uva":"Virginia Cavaliers","unc":"North Carolina Tar Heels","gatech":"Georgia Tech Yellow Jackets",
 "uw":"Washington Huskies","uiuc":"Illinois Fighting Illini","umd":"Maryland Terrapins","uf":"Florida Gators",
 "ut-austin":"Texas Longhorns","purdue":"Purdue Boilermakers","rutgers":"Rutgers Scarlet Knights","wisc":"Wisconsin Badgers",
 "washu":"Washington University Bears","uchicago":"Chicago Maroons","jhu":"Johns Hopkins Blue Jays","northeastern":"Northeastern Huskies",
 "swarthmore":"Swarthmore Garnet","williams":"Williams Ephs","amherst":"Amherst Mammoths","emory":"Emory Eagles","villanova":"Villanova Wildcats",
}
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA={"User-Agent":"CandorLogoFetch/1.0 (https://candoradmit.com; contact jasper)"}
_lock=threading.Lock(); _last=[0.0]
def get(u):
    with _lock:
        dt=time.time()-_last[0]
        if dt<0.3: time.sleep(0.3-dt)
        _last[0]=time.time()
    for _ in range(4):
        try: return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=40,context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.5); continue
            raise
    raise RuntimeError("rl")
def getj(u): return json.loads(get(u))
WP="https://en.wikipedia.org/w/api.php?"; WD="https://www.wikidata.org/w/api.php?"; CM="https://commons.wikimedia.org/w/api.php?"
def qid_for(t):
    d=getj(WP+urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item","redirects":"1","titles":t,"format":"json"}))
    p=d["query"]["pages"]; pp=p[list(p)[0]]; return (pp.get("pageprops") or {}).get("wikibase_item")
def claim_files(qid, props):
    out=[]
    for prop in props:
        try:
            d=getj(WD+urllib.parse.urlencode({"action":"wbgetclaims","entity":qid,"property":prop,"format":"json"}))
            for cl in d.get("claims",{}).get(prop,[]):
                try: out.append(cl["mainsnak"]["datavalue"]["value"])
                except: pass
        except: pass
    return out
def page_images(title):
    """fallback: image files used on the athletics Wikipedia page (catches logos
    not in a wikidata claim)."""
    try:
        d=getj(WP+urllib.parse.urlencode({"action":"query","prop":"images","titles":title,"imlimit":"30","redirects":"1","format":"json"}))
        p=d["query"]["pages"]; pp=p[list(p)[0]]
        return [im["title"].split("File:",1)[-1] for im in pp.get("images",[]) if any(k in im["title"].lower() for k in ("logo","wordmark","athletic","mark",".svg")) and not im["title"].lower().endswith((".pdf",))]
    except: return []
def thumb(fn,w=1700):
    d=getj(CM+urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url","iiurlwidth":str(w),"format":"json"}))
    p=d["query"]["pages"]; pp=p[list(p)[0]]; ii=(pp.get("imageinfo") or [{}])[0]; return ii.get("thumburl") or ii.get("url")
def sil(im,n=200):
    im=im.convert("RGBA"); bg=Image.new("RGBA",im.size,(255,255,255,255)); bg.alpha_composite(im)
    g=bg.convert("L").resize((n,n)); px=g.load()
    return [1 if px[x,y]<232 else 0 for y in range(n) for x in range(n)]
def iou(a,b):
    inter=sum(1 for x,y in zip(a,b) if x and y); uni=sum(1 for x,y in zip(a,b) if x or y)
    return inter/uni if uni else 0

slugs=sorted(app.INST_LOGOS)
OUT="/tmp/logo_up2"; shutil.rmtree(OUT,ignore_errors=True); os.makedirs(OUT)
res={}
def work(slug):
    cur=f"static/logos_inst/{slug}.png"
    if not os.path.exists(cur): return (slug,None,0)
    csil=sil(Image.open(cur))
    cands=[]
    # athletics entity first (athletic marks), then its page images
    team=TEAM.get(slug)
    if team:
        try:
            tq=qid_for(team)
            if tq: cands+=claim_files(tq,["P154"])
            cands+=page_images(team)
        except: pass
    # university entity (seal / coat of arms / logo) as fallback
    try:
        uq=qid_for((app.COLLEGES_BY_SLUG.get(slug) or {}).get("name") or slug)
        if uq: cands+=claim_files(uq,["P154","P158","P94"])
    except: pass
    seen=set(); best=(0,None,None)
    for fn in cands:
        if fn in seen: continue
        seen.add(fn)
        if len(seen)>10: break
        try:
            url=thumb(fn,1700)
            if not url: continue
            data=get(url)
            if len(data)<1500: continue
            sc=iou(csil,sil(Image.open(BytesIO(data))))
            if sc>best[0]: best=(sc,data,fn)
        except: continue
    sc,data,fn=best
    if data and sc>=0.6:
        open(f"{OUT}/{slug}.png","wb").write(data)
    return (slug,fn,round(sc,2))
with ThreadPoolExecutor(max_workers=4) as ex:
    for fut in as_completed([ex.submit(work,s) for s in slugs]):
        slug,fn,sc=fut.result(); res[slug]=(fn,sc)
        tag="MATCH" if sc>=0.6 else "----"
        print(f"  {slug:16} {tag} {sc}  {(fn or '')[:42]}")
matched=[s for s in slugs if res[s][1]>=0.6]
print(f"\nMATCHED: {len(matched)}/{len(slugs)} -> {matched}")
json.dump(res,open(f"{OUT}/_res.json","w"))
