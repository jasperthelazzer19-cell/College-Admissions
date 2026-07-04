#!/usr/bin/env python3
"""Convert a Candor carousel into a YouTube Short / Reel MP4.

Usage: python3 scripts/carousel_to_short.py <cid> [out.mp4]

Fetches the carousel's slides from the live site's public slide route,
lays each 2:3 slide on a dark 9:16 canvas with a slow zoom (so it reads
as video, not a dead image), and concatenates with quick crossfades.
Timing is retention-tuned: title short, profile long enough to read,
reveal held, CTA quick. Silent audio — add trending sound in-app when
posting (burned-in music is a copyright trap)."""
import os, subprocess, sys, tempfile, urllib.request

BASE = os.environ.get("SHORT_BASE", "https://candoradmit.com")
FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

def fetch_slides(cid):
    tmp = tempfile.mkdtemp(prefix="short_")
    paths = []
    for piece in ["1", "2", "3", "4", "cta"]:
        url = f"{BASE}/content/slide/{cid}/{piece}.jpg"
        p = os.path.join(tmp, f"{piece}.jpg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "candor-shorts"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    open(p, "wb").write(r.read())
                    paths.append(p)
        except Exception:
            pass                                   # missing piece (e.g. no slide 4)
    return paths

def build(cid, out):
    slides = fetch_slides(cid)
    assert len(slides) >= 3, f"only {len(slides)} slides found for cid {cid}"
    n = len(slides)
    # per-slide seconds: title, profile(read time), reveal(held), extras, CTA
    if n == 4:
        durs = [2.4, 4.6, 3.8, 2.2]
    elif n == 5:
        durs = [2.4, 4.2, 4.2, 3.4, 2.0]
    else:
        durs = [2.4, 4.6, 3.0]
    fade = 0.28
    inputs, filters = [], []
    for i, (p, d) in enumerate(zip(slides, durs)):
        frames = int(round((d + fade) * 30))
        inputs += ["-i", p]
        # fit onto the 9:16 canvas, then a slow zoom-in via zoompan
        filters.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x0a131c,"
            f"zoompan=z='min(1+0.0011*on,1.09)':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,"
            f"setsar=1,format=yuv420p[v{i}]")
    # chain crossfades
    chain, cur, offset = "", "v0", 0.0
    for i in range(1, n):
        offset += durs[i - 1]
        nxt = f"vx{i}" if i < n - 1 else "vout"
        chain += f"[{cur}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.2f}[{nxt}];"
        cur = nxt
    fc = ";".join(filters) + ";" + chain.rstrip(";")
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc, "-map", "[vout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21",
           "-movflags", "+faststart", "-t", f"{sum(durs) + fade:.2f}", out]
    subprocess.run(cmd, check=True, capture_output=True)
    return out, sum(durs)

if __name__ == "__main__":
    cid = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/candor_short_{cid}.mp4"
    path, secs = build(cid, out)
    print(f"{path}  ({secs:.0f}s, {os.path.getsize(path)//1024}KB)")
