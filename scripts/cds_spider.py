#!/usr/bin/env python3
"""Find each school's CDS by following links, not by guessing filenames.

Learned from 116 real CDS URLs the agents found: the files live at deep,
site-specific paths (www.x.edu/sites/default/files/CDS_2025-2026.pdf,
.../wp-content/uploads/..., .../media/documents/...). Guessing those is hopeless
— only 2 hits in 100 schools. But every one of them is LINKED from a findable
institutional-research page, so do what a person does: start at the domain,
follow links that look like IR / common-data-set, two levels deep, and grab the
first CDS PDF. Then read it with cds_read (no model, no search).

Usage: python3 scripts/cds_spider.py [--slugs a,b] [--workers 24] [--limit N]
Writes /tmp/cds/spider.json
"""
import concurrent.futures as cf, json, os, re, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import candor_data as cd
from scripts.cds_read import read

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122 Safari/537.36"}
DOMAINS = json.load(open("/tmp/cds/domains.json"))
# Link text / href fragments that lead toward a CDS, best first.
LEAD = re.compile(r"common[\s\-_]?data[\s\-_]?set|/cds\b|institutional[\s\-_]?research|"
                  r"institutional[\s\-_]?effectiveness|oira|\bir\b|fact[\s\-_]?book|"
                  r"institutional[\s\-_]?analytics|planning[\s\-_]?and[\s\-_]?analysis", re.I)
CDSPDF = re.compile(r"(cds|common[\-_ ]?data)", re.I)
YEAR = re.compile(r"20(2[3-9])[\-_]?(20)?(2[4-9])")


def get(url, timeout=10, cap=400000):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
    return r.read(cap), r.headers.get("Content-Type", "")


def links(html, base):
    out = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        out.append((urllib.parse.urljoin(base, href), text))
    return out


def score_pdf(url):
    """Prefer a CDS pdf, and among those prefer the most recent year."""
    if not CDSPDF.search(url):
        return -1
    m = YEAR.search(url)
    return (int(m.group(3)) if m else 0) + 10


def spider(slug):
    dom = DOMAINS.get(slug)
    if not dom:
        return {"slug": slug, "found": False, "notes": "no domain"}
    seen, frontier, pdfs = set(), [], []
    for seed in (f"https://www.{dom}/institutional-research", f"https://ir.{dom}",
                 f"https://oir.{dom}", f"https://oira.{dom}", f"https://ire.{dom}",
                 f"https://www.{dom}/common-data-set",
                 f"https://www.{dom}/institutional-effectiveness",
                 f"https://www.{dom}/about/institutional-research", f"https://www.{dom}"):
        frontier.append((seed, 0))
    while frontier and len(seen) < 45:
        url, depth = frontier.pop(0)
        if url in seen or depth > 3:
            continue
        seen.add(url)
        try:
            body, ctype = get(url)
        except Exception:
            continue
        if body[:5] == b"%PDF-":
            if score_pdf(url) > 0:
                pdfs.append(url)
            continue
        if "html" not in ctype.lower():
            continue
        html = body.decode("utf-8", "ignore")
        cand = []
        for href, text in links(html, url):
            if urllib.parse.urlparse(href).netloc.split(".")[-2:] != dom.split(".")[-2:]:
                continue
            if href.lower().endswith(".pdf"):
                if score_pdf(href) > 0:
                    pdfs.append(href)
            elif LEAD.search(href) or LEAD.search(text):
                cand.append((href, depth + 1))
        frontier = cand + frontier          # depth-first toward IR pages
    if not pdfs:
        return {"slug": slug, "found": False, "notes": f"crawled {len(seen)} pages, no CDS pdf"}
    # Newest edition wins. Crawling used to stop at the FIRST CDS pdf it saw,
    # which handed back Boston College's 2024-25 while its 2025-26 sat one link
    # further down the same page. Collect everything, then rank.
    ranked = sorted(set(pdfs), key=score_pdf, reverse=True)
    best = ranked[0]
    if score_pdf(best) < 25:          # edition year < 2025 -> too old to be an upgrade
        older = [u for u in ranked if score_pdf(u) >= 24]
        if not older:
            return {"slug": slug, "found": False,
                    "notes": f"only pre-2024-25 editions found ({len(ranked)} pdfs)"}
        best = older[0]
    try:
        rec = read(best)
    except Exception as e:
        return {"slug": slug, "found": False, "notes": f"read failed: {type(e).__name__}", "source_url": best}
    if not rec.get("found"):
        return {"slug": slug, "found": False, "notes": "pdf was not a CDS", "source_url": best}
    rec.update(slug=slug, source_url=best, via="spider")
    return rec


def main():
    done = set()
    for f in os.listdir("/tmp/cds"):
        if f.startswith(("out", "pilot")) and f.endswith(".json"):
            try:
                for r in json.load(open("/tmp/cds/" + f)):
                    if r.get("found"):
                        done.add(r["slug"])
            except Exception:
                pass
    if "--slugs" in sys.argv:
        want = sys.argv[sys.argv.index("--slugs") + 1].split(",")
    else:
        want = [c["slug"] for c in cd.COLLEGES if c["slug"] not in done]
    if "--limit" in sys.argv:
        want = want[:int(sys.argv[sys.argv.index("--limit") + 1])]
    n = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 24
    print(f"spidering {len(want)} schools, {n} workers", flush=True)
    out, hit = [], 0
    sink = open("/tmp/cds/spider.jsonl", "a")
    with cf.ThreadPoolExecutor(n) as ex:
        futs = {ex.submit(spider, s_): s_ for s_ in want}
        for i, fu in enumerate(cf.as_completed(futs), 1):
            try:
                r = fu.result()
            except Exception as e:
                r = {"slug": futs[fu], "found": False, "notes": type(e).__name__}
            out.append(r)
            sink.write(json.dumps(r) + "\n"); sink.flush()
            if r.get("found"):
                hit += 1
                print(f"  [{i}/{len(want)}] {r['slug']:22s} OK {r.get('cds_year') or '?'} "
                      f"accept={r.get('accept')} c7={'y' if r.get('c7') else 'n'}", flush=True)
            elif i % 25 == 0:
                print(f"  [{i}/{len(want)}] … {hit} found so far", flush=True)
    json.dump(out, open("/tmp/cds/spider.json", "w"), indent=1)
    print(f"\nspider: {hit}/{len(out)} found; "
          f"{sum(1 for r in out if r.get('c7'))} with C7")


if __name__ == "__main__":
    main()
