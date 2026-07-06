from PIL import Image, ImageDraw
import os

SRC = "/Users/jasperlasser/Downloads/High-resolution_professional_collage_of_university_202606142011.jpeg"
im = Image.open(SRC).convert("RGBA")
W, H = im.size                       # 2752 x 1536

# title strip at top + caption strip at bottom to exclude
TITLE_H = int(H * 0.105)
CAP_H   = int(H * 0.045)
BODY0, BODY1 = TITLE_H, H - CAP_H
ROWS = 5
rh = (BODY1 - BODY0) / ROWS

GRID = [
 ["harvard","yale","princeton","columbia","upenn","brown","dartmouth","cornell","stanford","mit"],
 ["ucla","ucb","uva","unc","usc","nyu","caltech","northwestern","umich","wisc"],
 ["tulane","duke","jhu","uchicago","notre-dame","vanderbilt","washu","rice","cmu","emory"],
 ["gatech","georgetown","tufts","wake-forest","bu","villanova","ut-austin","uf","northeastern","miami"],
 ["bc","ucsd","purdue","uiuc","uw","umd","rutgers","williams","amherst","swarthmore"],
]

LABEL_FRAC = 0.34     # bottom of each row band = label text, skip it
WHITE = 232           # >= this on all channels = background (also kills light gridlines)
MIN_GAP = 26          # white gap smaller than this = same logo
MIN_INK_COL = 6       # a column needs > this many ink px to count (ignores thin gridlines)

px = im.load()
def is_ink(x, y):
    r, g, b, a = px[x, y]
    return a > 25 and not (r >= WHITE and g >= WHITE and b >= WHITE)

def segment_row(r):
    y0 = int(BODY0 + r * rh) + 6
    y1 = int(BODY0 + r * rh + rh * (1 - LABEL_FRAC))
    ink = [sum(1 for y in range(y0, y1) if is_ink(x, y)) for x in range(W)]
    cols = [ink[x] > MIN_INK_COL for x in range(W)]
    runs = []; x = 0
    while x < W:
        if cols[x]:
            s = x
            while x < W and cols[x]: x += 1
            e = x
            while True:
                g = x
                while g < W and not cols[g]: g += 1
                if g < W and (g - e) < MIN_GAP:
                    while g < W and cols[g]: g += 1
                    e = g; x = g
                else:
                    break
            runs.append((s, e, y0, y1))
        else:
            x += 1
    return [r for r in runs if (r[1] - r[0]) >= 12]

GAP_V = 15   # vertical white gap separating the logo from its label below
def trim_v(box):
    x0, x1, y0, y1 = box
    # row ink profile within this logo's column range
    rows_ink = [any(is_ink(x, y) for x in range(x0, x1)) for y in range(y0, y1)]
    # find the FIRST vertical run of ink (the logo), merging internal gaps < GAP_V;
    # this stops at the bigger gap before the label text, excluding the label.
    i = 0; n = len(rows_ink)
    while i < n and not rows_ink[i]: i += 1
    if i >= n: return None
    s = i
    while i < n:
        while i < n and rows_ink[i]: i += 1
        e = i
        g = i
        while g < n and not rows_ink[g]: g += 1
        if g < n and (g - e) < GAP_V:
            i = g
        else:
            break
    return (x0, y0 + max(0, s-4), x1, y0 + min(n, e+5))

def whiten(img, t=236):
    p = img.load(); w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = p[x, y]
            if r > t and g > t and b > t: p[x, y] = (r, g, b, 0)
    return img

os.makedirs("/tmp/x2", exist_ok=True)
for f in os.listdir("/tmp/x2"): os.remove(f"/tmp/x2/{f}")
report = []
for r in range(ROWS):
    runs = segment_row(r); slugs = GRID[r]
    report.append(f"row {r}: {len(runs)} runs vs {len(slugs)} slugs")
    for i, run in enumerate(runs):
        if i >= len(slugs): break
        v = trim_v(run)
        if not v: continue
        x0, y0, x1, y1 = v
        whiten(im.crop((x0, y0, x1, y1))).save(f"/tmp/x2/{slugs[i]}.png")
print("\n".join(report))

order = [s for row in GRID for s in row]
cols = 6; cell = 230; nrows = (len(order)+cols-1)//cols
for name, bg in (("white",(255,255,255)),("gray",(128,128,136))):
    sheet = Image.new("RGB", (cols*cell, nrows*cell), bg); d = ImageDraw.Draw(sheet)
    for i, slug in enumerate(order):
        x=(i%cols)*cell; y=(i//cols)*cell
        d.rectangle([x,y,x+cell-1,y+cell-1],outline=(170,170,170))
        p=f"/tmp/x2/{slug}.png"
        if os.path.exists(p):
            lg=Image.open(p).convert("RGBA"); lg.thumbnail((cell-34,cell-58))
            base=Image.new("RGBA",(cell-4,cell-4),bg+(255,)); base.alpha_composite(lg,((cell-4-lg.width)//2,(cell-4-lg.height)//2-8))
            sheet.paste(base.convert("RGB"),(x+2,y+2))
        d.text((x+6,y+cell-15),slug,fill=(255,255,0) if name=="gray" else (10,10,10))
    sheet.save(f"/tmp/x2_sheet_{name}.png")
print("sheets written")
