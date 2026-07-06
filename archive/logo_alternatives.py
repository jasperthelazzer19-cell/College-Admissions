"""For each not-yet-upgraded INST_LOGO school, gather up to 4 hi-res logo
alternatives (Wikimedia P154 logo / P158 seal / P94 coat-of-arms claims + the
local athletic logo) and lay them out numbered next to the current logo, so
Jasper can pick one per school. Saves candidates to /tmp/alts/<slug>_<n>.png and
builds grid pages /tmp/alts_page_*.png."""
import json, os, ssl, time, threading, urllib.request, urllib.parse, sys, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw, ImageFont
import warnings; warnings.filterwarnings("ignore")
sys.stderr=open(os.devnull,"w"); import app; sys.stderr=sys.__stderr__
from hires_logos import TITLES as FEAT

ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
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
def thumb(fn,w=620):
    d=getj(CM+urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url","iiurlwidth":str(w),"format":"json"}))
    p=d["query"]["pages"];pp=p[list(p)[0]];ii=(pp.get("imageinfo") or [{}])[0];return ii.get("thumburl") or ii.get("url")

DONE={"cornell","harvard","princeton","ucsd","yale"}
slugs=sorted(s for s in app.INST_LOGOS if s not in DONE)
ALT="/tmp/alts"; shutil.rmtree(ALT,ignore_errors=True); os.makedirs(ALT)

def gather(slug):
    paths=[]
    # athletic logo (local) as one alternative
    ath=f"static/logos/{slug}.png"
    if os.path.exists(ath):
        p=f"{ALT}/{slug}_ath.png"; shutil.copy(ath,p); paths.append(p)
    try:
        qid=qid_for(title_for(slug))
        for i,fn in enumerate(files(qid) if qid else []):
            if len(paths)>=4: break
            try:
                url=thumb(fn,620);
                if not url: continue
                data=get(url)
                if len(data)<1200: continue
                p=f"{ALT}/{slug}_{i}.png"; open(p,"wb").write(data); paths.append(p)
            except: continue
    except: pass
    return (slug, paths[:4])

res={}
with ThreadPoolExecutor(max_workers=4) as ex:
    for fut in as_completed([ex.submit(gather,s) for s in slugs]):
        s,ps=fut.result(); res[s]=ps; print(f"  {s}: {len(ps)} alts")

# build pages: each row = slug | CURRENT | #1 #2 #3 #4
def onwhite(path,box):
    im=Image.open(path).convert("RGBA");bg=Image.new("RGBA",im.size,(255,255,255,255));bg.alpha_composite(im);im=bg.convert("RGBA");im.thumbnail(box);return im
CELL=210; LBL=150; ROWH=210; PERPAGE=10
try: fB=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf",30); fN=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf",40); fS=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf",18)
except: fB=fN=fS=ImageFont.load_default()
W=LBL+CELL*5
pages=[]
for pi in range(0,len(slugs),PERPAGE):
    chunk=slugs[pi:pi+PERPAGE]
    sheet=Image.new("RGB",(W,ROWH*len(chunk)),(245,245,247)); d=ImageDraw.Draw(sheet)
    for ri,slug in enumerate(chunk):
        y=ri*ROWH
        d.rectangle([0,y,W-1,y+ROWH-2],fill=(255,255,255),outline=(210,210,210))
        d.text((10,y+ROWH//2-20),slug,fill=(15,15,15),font=fB)
        # current
        d.text((LBL+8,y+6),"current",fill=(150,150,150),font=fS)
        try:
            cu=onwhite(f"static/logos_inst/{slug}.png",(CELL-26,ROWH-54)); sheet.paste(cu.convert("RGB"),(LBL+12,y+30),cu)
        except: pass
        d.line([LBL+CELL,y+4,LBL+CELL,y+ROWH-6],fill=(160,160,160))
        # alts numbered 1-4
        for ai in range(4):
            cx=LBL+CELL*(ai+1)
            d.text((cx+8,y+6),f"{ai+1}",fill=(40,90,200),font=fN)
            ps=res.get(slug,[])
            if ai<len(ps):
                try:
                    al=onwhite(ps[ai],(CELL-26,ROWH-58)); sheet.paste(al.convert("RGB"),(cx+12,y+44),al)
                except: pass
            else:
                d.text((cx+12,y+ROWH//2),"—",fill=(200,200,200),font=fB)
            d.line([cx,y+4,cx,y+ROWH-6],fill=(230,230,230))
    fn=f"/tmp/alts_page_{pi//PERPAGE+1}.png"; sheet.save(fn); pages.append(fn)
print("PAGES:", pages)
