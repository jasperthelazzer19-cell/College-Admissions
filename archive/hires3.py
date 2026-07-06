"""Targeted fetch for the shield/crest schools (graphic uses the institutional
shield, not the athletic letter) + fix MIT (broken) and Emory (failed). Uses
Commons file search, takes the best SVG/PNG logo result, renders at 1024px."""
import json, os, ssl, time, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageDraw

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA={"User-Agent":"CandorLogoFetch/1.0 (https://candoradmit.com; jasper)"}
_lock=threading.Lock(); _last=[0.0]
def get(u):
    with _lock:
        dt=time.time()-_last[0]
        if dt<0.34: time.sleep(0.34-dt)
        _last[0]=time.time()
    for _ in range(4):
        try: return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=30,context=ctx).read()
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.5); continue
            raise
    raise RuntimeError("rl")
def getj(u): return json.loads(get(u))
CM="https://commons.wikimedia.org/w/api.php?"

# direct Commons filenames (high-confidence) for each
FILES = {
 "harvard":["Harvard Veritas shield.svg","Harvard shield-Business.svg","Harvard University shield.svg"],
 "brown":["Brown University coat of arms.svg","Brown University shield.svg"],
 "princeton":["Princeton shield.svg","Shield of Princeton University.svg","Princeton University shield.svg"],
 "upenn":["UPenn shield with wordmark.svg","University of Pennsylvania shield.svg","Pennsylvania Quakers logo.svg"],
 "jhu":["Johns Hopkins University coat of arms.svg","Johns Hopkins University shield.svg"],
 "columbia":["Columbia University shield.svg","Columbia coat of arms.svg"],
 "cornell":["Cornell University seal.svg","Cornell University Seal.svg"],
 "uchicago":["University of Chicago shield.svg","University of Chicago coat of arms.svg"],
 "mit":["MIT logo.svg","MIT Logo.svg","Massachusetts Institute of Technology seal.svg"],
 "emory":["Emory University logo.svg","Emory Eagles logo.svg","Emory University Eagles logo.svg"],
}
def search(query):
    d=getj(CM+urllib.parse.urlencode({"action":"query","list":"search","srsearch":"File:"+query+" logo","srnamespace":"6","srlimit":"8","format":"json"}))
    return [r["title"].split("File:",1)[-1] for r in d.get("query",{}).get("search",[])]
def png(fn,w=1024):
    d=getj(CM+urllib.parse.urlencode({"action":"query","titles":"File:"+fn,"prop":"imageinfo","iiprop":"url|mime","iiurlwidth":str(w),"format":"json"}))
    p=d["query"]["pages"]; pp=p[list(p)[0]]
    if "-1" in str(list(p)[0]) and "missing" in pp: return None
    ii=(pp.get("imageinfo") or [{}])[0]; return ii.get("thumburl") or ii.get("url")

os.makedirs("/tmp/hi3",exist_ok=True)
def work(slug):
    cands=list(FILES.get(slug,[]))
    # add search results as fallback
    try: cands += search(slug.replace("-"," ")+" university shield")
    except Exception: pass
    for fn in cands:
        try:
            u=png(fn,1024)
            if not u: continue
            data=get(u)
            if len(data)<2000: continue
            open(f"/tmp/hi3/{slug}.png","wb").write(data)
            return (slug,f"ok {len(data)//1024}KB {fn[:40]}")
        except Exception: continue
    return (slug,"none")

res={}
with ThreadPoolExecutor(max_workers=3) as ex:
    for f in as_completed([ex.submit(work,s) for s in FILES]):
        s,m=f.result(); res[s]=m
for s in sorted(res): print(s,res[s])

# compare graphic vs hi3
order=sorted(FILES)
PW=150;CELL=PW*2+10;H=170;cols=3;rows=(len(order)+cols-1)//cols
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
    place(f"/tmp/hi3/{slug}.png",cx+PW+10,cy,"NEW")
sh.save("/tmp/hi3_compare.png");print("sheet -> /tmp/hi3_compare.png")
