"""Build a comparison grid: CURRENT logo (left) vs the best hi-res Wikimedia
candidate (right) for each INST_LOGO not already upgraded, so we can decide which
to swap. Pulls P154/P158/P94, picks the candidate closest in shape to the current
logo. Output: /tmp/logo_grid_*.png"""
import json, os, ssl, time, threading, urllib.request, urllib.parse, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont
import warnings; warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w"); import app; sys.stderr = sys.__stderr__
from hires_logos import TITLES as FEAT

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA={"User-Agent":"CandorLogoFetch/1.0 (https://candoradmit.com; contact jasper)"}
_lock=threading.Lock(); _last=[0.0]
def get(u):
    with _lock:
        dt=time.time()-_last[0]
        if dt<0.34: time.sleep(0.34-dt)
        _last[0]=time.time()
    for _ in range(4):
        try: return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=40,context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.5); continue
            raise
    raise RuntimeError("rl")
def getj(u): return json.loads(get(u))
WP="https://en.wikipedia.org/w/api.php?";WD="https://www.wikidata.org/w/api.php?";CM="https://commons.wikimedia.org/w/api.php?"
def title_for(s): return FEAT.get(s) or (app.COLLEGES_BY_SLUG.get(s) or {}).get("name") or s
def qid_for(t):
    d=getj(WP+urllib.parse.urlencode({"action":"query","prop":"pageprops","ppprop":"wikibase_item","redirects":"1","titles":t,"format":"json"}))
    p=d["query"]["pages"];pp=p[list(p)[0]];return (pp.get("pageprops") or {}).get("wikibase_item")
def files(qid):
    out=[]
    for prop in ("P154","P158","P94"):
        try:
            d=getj(WD+urllib.parse.urlencode({"action":"wbgetclaims","entity":qid,"property":prop,"format":"json"}))
            for cl in d.get("claims",{}).get(prop,[]):
                try: out.append(cl["mainsnak"]["datavalue"]["value"])
                except: pass
        except: pass
    return list(dict.fromkeys(out))
def thumb(fn,w=520):
    d=getj(CM+urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url|size","iiurlwidth":str(w),"format":"json"}))
    p=d["query"]["pages"];pp=p[list(p)[0]];ii=(pp.get("imageinfo") or [{}])[0];return ii.get("thumburl") or ii.get("url")

# the 12 already upgraded — skip; show the rest
DONE={"cornell","harvard","princeton","columbia","mit","upenn","uva","wisc","yale","ucla","ucb","ucsd"}
slugs=[s for s in app.INST_LOGOS if s not in DONE]
TMP="/tmp/logo_props"; os.makedirs(TMP,exist_ok=True)

def cur_ar(slug):
    try:
        with Image.open(f"static/logos_inst/{slug}.png") as im: return im.width/im.height
    except: return None
def fetch_prop(slug):
    try:
        qid=qid_for(title_for(slug))
        if not qid: return (slug,None,"no-qid")
        car=cur_ar(slug); best=None
        for i,fn in enumerate(files(qid)):
            url=thumb(fn,520)
            if not url: continue
            try: data=get(url)
            except: continue
            if len(data)<1200: continue
            p=f"{TMP}/{slug}__{i}.png"; open(p,"wb").write(data)
            try:
                with Image.open(p) as im: w,h=im.size
            except: continue
            ar=w/h; diff=abs(ar-car)/car if car else 0
            if best is None or diff<best[0]: best=(diff,p,fn,max(w,h))
        if best: return (slug,best[1],f"{best[3]}px d{best[0]:.2f}")
        return (slug,None,"no-file")
    except Exception as e: return (slug,None,"ERR"+str(e)[:30])

props={}
with ThreadPoolExecutor(max_workers=4) as ex:
    for fut in as_completed([ex.submit(fetch_prop,s) for s in slugs]):
        s,p,note=fut.result(); props[s]=(p,note); print(f"  {s}: {note}")

# compose grid: tiles, each = slug + CURRENT (top) + PROPOSED (bottom)
def load_on_white(path, box):
    im=Image.open(path).convert("RGBA"); bg=Image.new("RGBA",im.size,(255,255,255,255)); bg.alpha_composite(im); im=bg.convert("RGBA")
    im.thumbnail(box); return im
TW,TH=300,300; COLS=5
rows=(len(slugs)+COLS-1)//COLS
sheet=Image.new("RGB",(COLS*TW, rows*TH),(245,245,245)); d=ImageDraw.Draw(sheet)
try: f=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf",18); fs=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf",13)
except: f=ImageFont.load_default(); fs=f
for i,slug in enumerate(sorted(slugs)):
    x=(i%COLS)*TW; y=(i//COLS)*TH
    d.rectangle([x,y,x+TW-2,y+TH-2],fill=(255,255,255),outline=(210,210,210))
    d.text((x+8,y+6),slug,fill=(15,15,15),font=f)
    d.text((x+10,y+30),"NOW",fill=(150,150,150),font=fs)
    d.text((x+TW//2+10,y+30),"NEW",fill=(150,150,150),font=fs)
    d.line([x+TW//2,y+28,x+TW//2,y+TH-6],fill=(225,225,225))
    try:
        cu=load_on_white(f"static/logos_inst/{slug}.png",(TW//2-24,TH-70))
        sheet.paste(cu.convert("RGB"),(x+10,y+50),cu)
    except: pass
    pp,note=props.get(slug,(None,""))
    if pp and os.path.exists(pp):
        try:
            pr=load_on_white(pp,(TW//2-24,TH-70)); sheet.paste(pr.convert("RGB"),(x+TW//2+12,y+50),pr)
            d.text((x+TW//2+10,y+TH-18),note[:22],fill=(120,120,120),font=fs)
        except: pass
    else:
        d.text((x+TW//2+10,y+TH//2),note[:18],fill=(190,0,0),font=fs)
# split into 2 images if tall
sheet.save("/tmp/logo_grid_full.png")
print("rows",rows,"-> /tmp/logo_grid_full.png", sheet.size)
