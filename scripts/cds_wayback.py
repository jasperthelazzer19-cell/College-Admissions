#!/usr/bin/env python3
"""Last-resort CDS finder: the Internet Archive's CDX index.

Catches the categories the live-site spider structurally cannot:
  - schools that 403 every non-browser client (Gonzaga, Emerson, Akron...)
  - JS-only IR pages that serve no links to a static fetch
  - files the school moved or deleted since publishing

The CDX API is free and unmetered, indexes PDFs, and lets us filter to a domain
plus a URL pattern in one request. We ask for every archived *.pdf on the
school's domain whose URL looks like a CDS, prefer the most recent edition, and
pull it from the archive's own copy (id_ so we get the raw file, not the
Wayback-wrapped HTML). Then read it with cds_read — no model, no search.

Usage: python3 scripts/cds_wayback.py [--slugs a,b] [--workers 12]
Writes /tmp/cds/wayback.json
"""
import concurrent.futures as cf, json, os, re, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import candor_data as cd
from scripts.cds_read import read

UA = {"User-Agent": "candor-cds-research/1.0"}
DOMAINS = json.load(open("/tmp/cds/domains.json"))
CDX = "https://web.archive.org/cdx/search/cdx"
YEAR = re.compile(r"20(2[0-9])[\-_]?(?:20)?(2[0-9])")


def edition(url):
    """Sort key: the later the CDS edition in the filename, the better."""
    m = YEAR.search(url)
    return int(m.group(2)) if m else 0


def candidates(dom):
    """Archived CDS-looking files on this domain, newest edition first.

    Two things the obvious query gets wrong: CDX needs matchType=domain (a
    'dom/*' url pattern 503s), and filtering to '.pdf' silently drops real CDS
    files — Gonzaga serves theirs as .ashx. So filter on the URL looking like a
    CDS and let the content sniff decide whether it's really a PDF.
    """
    q = urllib.parse.urlencode({
        "url": dom, "matchType": "domain", "output": "json", "limit": "800",
        # CDX rejects an inline (?i) flag with a 503 — spell the case out.
        "filter": "original:.*([Cc][Dd][Ss]|[Cc]ommon.?[Dd]ata).*",
        "collapse": "urlkey", "fl": "timestamp,original,statuscode",
    })
    try:
        raw = urllib.request.urlopen(urllib.request.Request(f"{CDX}?{q}", headers=UA),
                                     timeout=60).read().decode("utf-8", "ignore")
        rows = json.loads(raw)[1:]
    except Exception:
        return []
    out = [(ts, u) for ts, u, sc in rows if sc == "200"
           and not u.lower().endswith((".html", ".htm", ".aspx", "/"))]
    out.sort(key=lambda r: (edition(r[1]), r[0]), reverse=True)
    return out[:8]


def one(slug):
    dom = DOMAINS.get(slug)
    if not dom:
        return {"slug": slug, "found": False, "notes": "no domain"}
    for ts, url in candidates(dom):
        snap = f"https://web.archive.org/web/{ts}id_/{url}"
        try:
            rec = read(snap)
        except Exception:
            continue
        if not rec.get("found"):
            continue
        # A stale archived edition is worse than what Candor already holds, so
        # never accept one older than 2023-2024. (Gonzaga's newest archived CDS
        # is 2021-2022 — finding it is not the same as improving on it.)
        yr = rec.get("cds_year") or ""
        m2 = re.match(r"(20\d\d)", yr)
        if not m2 or int(m2.group(1)) < 2023:
            rec_year_note = yr or "unknown"
            continue
        rec.update(slug=slug, source_url=url, archived_url=snap, via="wayback")
        return rec
    return {"slug": slug, "found": False, "via": "wayback", "notes": "no CDS pdf in archive"}


def main():
    done = set()
    for f in os.listdir("/tmp/cds"):
        if f.endswith(".json") and f.startswith(("out", "pilot", "spider", "crawl")):
            try:
                d = json.load(open("/tmp/cds/" + f))
                for r in (d if isinstance(d, list) else []):
                    if r.get("found"):
                        done.add(r["slug"])
            except Exception:
                pass
    want = (sys.argv[sys.argv.index("--slugs") + 1].split(",") if "--slugs" in sys.argv
            else [c["slug"] for c in cd.COLLEGES if c["slug"] not in done])
    n = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 10
    print(f"wayback: {len(want)} schools still missing, {n} workers", flush=True)
    out, hit = [], 0
    with cf.ThreadPoolExecutor(n) as ex:
        futs = {ex.submit(one, s): s for s in want}
        for i, fu in enumerate(cf.as_completed(futs), 1):
            r = fu.result()
            out.append(r)
            if r.get("found"):
                hit += 1
                print(f"  [{i}/{len(want)}] {r['slug']:22s} OK {r.get('cds_year') or '?'} "
                      f"accept={r.get('accept')} c7={'y' if r.get('c7') else 'n'}", flush=True)
            elif i % 25 == 0:
                print(f"  [{i}/{len(want)}] … {hit} found", flush=True)
    json.dump(out, open("/tmp/cds/wayback.json", "w"), indent=1)
    print(f"\nwayback: {hit}/{len(out)} found; {sum(1 for r in out if r.get('c7'))} with C7")


if __name__ == "__main__":
    main()
