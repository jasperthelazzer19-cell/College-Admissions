from PIL import Image, ImageDraw
import os

SRC = "/Users/jasperlasser/.claude/uploads/11e0b2dd-68ea-4992-9f1b-a51a38be30f1/d1dd0e37-230037A17FC1427180152119829D7BE2.png"
im = Image.open(SRC).convert("RGBA")
W, H = im.size                       # 1536 x 1024
ROWS = 5
rh = H / ROWS

# left-to-right slug order within each row
GRID = [
 ["harvard","yale","princeton","columbia","upenn","brown","dartmouth","cornell","mit","stanford"],
 ["ucla","ucb","uva","unc","usc","nyu","caltech","northwestern","umich","wisc"],
 ["tulane","duke","jhu","uchicago","notre-dame","vanderbilt","wustl","rice","cmu","emory"],
 ["gatech","georgetown","tufts","wake-forest","bu","villanova","ut-austin","uf","northeastern","miami"],
 ["bc","ucsd"],
]

LABEL_FRAC = 0.22      # bottom 22% of a row band = the text label, skip it
TOP0 = 52              # row 0 extra top skip ("THE IVIES" header + divider)
WHITE = 240            # >= this on all channels counts as background
MIN_GAP = 20           # white gap (px) smaller than this = same logo (merge)
MIN_INK_COL = 2        # a column needs > this many ink px to count as ink

px = im.load()
def is_ink(x, y):
    r, g, b, a = px[x, y]
    return a > 25 and not (r >= WHITE and g >= WHITE and b >= WHITE)

def segment_row(r):
    y0 = int(r * rh) + (TOP0 if r == 0 else 4)
    # row 4 (bc/ucsd) has extra vertical room so its label sits inside the band;
    # crop more aggressively there to drop the "BOSTON COLLEGE" label text.
    lf = 0.42 if r == 4 else LABEL_FRAC
    y1 = int(r * rh + rh * (1 - lf))
    # column ink profile
    ink = [sum(1 for y in range(y0, y1) if is_ink(x, y)) for x in range(W)]
    cols = [ink[x] > MIN_INK_COL for x in range(W)]
    # find runs, merging gaps < MIN_GAP
    runs = []
    x = 0
    while x < W:
        if cols[x]:
            s = x
            while x < W and cols[x]: x += 1
            e = x
            # peek ahead: merge if next run starts within MIN_GAP
            while True:
                g = x
                while g < W and not cols[g]: g += 1
                if g < W and (g - e) < MIN_GAP:
                    s2 = g
                    while g < W and cols[g]: g += 1
                    e = g; x = g
                else:
                    break
            runs.append((s, e, y0, y1))
        else:
            x += 1
    # drop sub-8px slivers (stray lines / antialiasing noise between logos)
    return [r for r in runs if (r[1] - r[0]) >= 8]

def trim_v(box):
    x0, x1, y0, y1 = box
    miny, maxy = y1, y0
    for y in range(y0, y1):
        if any(is_ink(x, y) for x in range(x0, x1)):
            miny = min(miny, y); maxy = max(maxy, y)
    if maxy <= miny: return None
    return (x0, max(y0, miny-3), x1, min(y1, maxy+4))

def whiten(img, t=243):
    p = img.load(); w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = p[x, y]
            if r > t and g > t and b > t: p[x, y] = (r, g, b, 0)
    return img

os.makedirs("/tmp/crops", exist_ok=True)
report = []
for r in range(ROWS):
    runs = segment_row(r)
    slugs = GRID[r]
    report.append(f"row {r}: {len(runs)} runs vs {len(slugs)} slugs")
    for i, run in enumerate(runs):
        if i >= len(slugs): break
        v = trim_v(run)
        if not v: continue
        x0, y0, x1, y1 = v
        crop = whiten(im.crop((x0, y0, x1, y1)))
        crop.save(f"/tmp/crops/{slugs[i]}.png")

print("\n".join(report))

# contact sheets (white + gray) for verification
order = [s for row in GRID for s in row]
cols = 6; cell = 230; nrows = (len(order) + cols - 1) // cols
for name, bg in (("white", (255,255,255)), ("gray", (128,128,136))):
    sheet = Image.new("RGB", (cols*cell, nrows*cell), bg)
    d = ImageDraw.Draw(sheet)
    for i, slug in enumerate(order):
        x = (i % cols) * cell; y = (i // cols) * cell
        d.rectangle([x, y, x+cell-1, y+cell-1], outline=(170,170,170))
        p = f"/tmp/crops/{slug}.png"
        if os.path.exists(p):
            lg = Image.open(p).convert("RGBA"); lg.thumbnail((cell-34, cell-58))
            base = Image.new("RGBA", (cell-4, cell-4), bg + (255,))
            base.alpha_composite(lg, ((cell-4-lg.width)//2, (cell-4-lg.height)//2 - 8))
            sheet.paste(base.convert("RGB"), (x+2, y+2))
        d.text((x+6, y+cell-15), slug, fill=(255,255,0) if name=="gray" else (10,10,10))
    sheet.save(f"/tmp/crops_sheet_{name}.png")
print("sheets written")
