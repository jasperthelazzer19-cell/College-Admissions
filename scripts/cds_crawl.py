#!/usr/bin/env python3
"""Find and read CDS documents WITHOUT web search or an LLM.

Web search turned out to be a session-wide budget shared across parallel agents,
so fanning out agents to *find* CDS files does not scale. But CDS URLs are
highly patterned: almost every school parks the file under an institutional
-research host at a predictable path. This tries those patterns directly with
plain HTTP, then reads whatever it lands with the deterministic C7 extractor and
a regex pass over C1/C9.

No model, no search quota, no cost. Schools it misses are handed to agents.

Usage:
  python3 scripts/cds_crawl.py --slugs a,b,c        # specific schools
  python3 scripts/cds_crawl.py --all --workers 12   # everything not already done
Writes /tmp/cds/crawl.json (same record shape the agents emit) + crawl.log
"""
import concurrent.futures as cf, io, json, os, re, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import candor_data as cd
from scripts.cds_c7_extract import extract as c7_extract

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122 Safari/537.36"}
CACHE = "/tmp/cds/cache"
YEARS = ["2025-2026", "2025-26", "2024-2025", "2024-25"]
# Pruned to the highest-yield shapes. The first cut tried ~184 URLs per school
# (8 hosts x 23 paths x 4 years) = ~73k requests and was projected at 2+ hours.
# These are landing pages that LINK to the PDF, which is both far fewer requests
# and more robust than guessing the file name.
HOSTS = ["ir", "oir", "www"]
PATHS = [
    "/common-data-set", "/cds",
    "/institutional-research/common-data-set",
    "/ir/common-data-set",
    "/about/institutional-research/common-data-set",
    "/institutional-research",
]
PDF_RE = re.compile(r'href=["\']([^"\']*(?:cds|common[-_]?data)[^"\']*\.pdf)["\']', re.I)


def _get(url, timeout=9):
    key = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9]+", "_", url)[:180])
    if os.path.exists(key) and os.path.getsize(key) > 0:
        return open(key, "rb").read()
    req = urllib.request.Request(url, headers=UA)
    data = urllib.request.urlopen(req, timeout=timeout).read()
    os.makedirs(CACHE, exist_ok=True)
    open(key, "wb").write(data)
    return data


_DOMAINS = json.load(open("/tmp/cds/domains.json"))


def _domain(slug):
    """Institution web domain. Resolved once by /tmp/cds/resolve_dom.py (candidate
    domains derived from the slug, verified by actually fetching them) plus a
    hand-filled table for the ~40 whose domain isn't derivable (uva ->
    virginia.edu, wake-forest -> wfu.edu, ...)."""
    return _DOMAINS.get(slug)


def candidates(slug):
    dom = _domain(slug)
    if not dom:
        return []
    out = []
    for h in HOSTS:
        base = f"https://{h}.{dom}" if h != "www" else f"https://www.{dom}"
        for p in PATHS:
            if "{y}" in p:
                out += [base + p.format(y=y) for y in YEARS]
            else:
                out.append(base + p)
    return out


def parse_pdf(data, url):
    """C1/C9 numbers by regex + C7 by geometry. Returns {} if it isn't a CDS."""
    import pdfplumber
    rec, txt = {}, ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pg in pdf.pages[:40]:
            txt += (pg.extract_text() or "") + "\n"
    if "Common Data Set" not in txt and "Relative importance" not in txt:
        return {}
    m = re.search(r"Total first-time,?\s*first-year.*?applicants?.*?([\d,]{3,})", txt, re.I | re.S)
    ap = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"Total first-time,?\s*first-year.*?admitted.*?([\d,]{3,})", txt, re.I | re.S)
    ad = int(m.group(1).replace(",", "")) if m else None
    if ap and ad and ad <= ap:
        rec.update(applicants=ap, admitted=ad, accept=round(ad / ap, 4))
    for label, keys in (("SAT Composite", ("sat_25", "sat_75")),
                        ("ACT Composite", ("act_25", "act_75"))):
        m = re.search(label + r"[^\n]*?(\d{2,4})\s+(\d{2,4})", txt)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi:
                rec[keys[0]], rec[keys[1]] = lo, hi
    try:
        c7, _ = c7_extract(io.BytesIO(data))
        if len(c7) >= 12:
            rec["c7"] = c7
    except Exception:
        pass
    for y in YEARS:
        if y in txt[:4000] or y in url:
            rec["cds_year"] = y
            break
    return rec


def try_school(slug, name):
    for url in candidates(slug):
        try:
            data = _get(url)
        except Exception:
            continue
        if data[:5] == b"%PDF-":
            rec = parse_pdf(data, url)
            if rec:
                return dict(slug=slug, found=True, source_url=url, via="crawl", **rec)
            continue
        # landing page: follow the first CDS-looking PDF link on it
        html = data.decode("utf-8", "ignore")
        for href in PDF_RE.findall(html)[:6]:
            full = urllib.parse.urljoin(url, href)
            try:
                pdf = _get(full)
            except Exception:
                continue
            if pdf[:5] != b"%PDF-":
                continue
            rec = parse_pdf(pdf, full)
            if rec:
                return dict(slug=slug, found=True, source_url=full, via="crawl", **rec)
    return dict(slug=slug, found=False, via="crawl", notes="no CDS at any known URL pattern")


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
        want = set(sys.argv[sys.argv.index("--slugs") + 1].split(","))
    else:
        want = {c["slug"] for c in cd.COLLEGES} - done
    todo = [(c["slug"], c["name"]) for c in cd.COLLEGES if c["slug"] in want]
    n = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 10
    print(f"crawling {len(todo)} schools with {n} workers (skipping {len(done)} already found)")
    out = []
    with cf.ThreadPoolExecutor(n) as ex:
        futs = {ex.submit(try_school, s, nm): s for s, nm in todo}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:
                r = dict(slug=futs[fut], found=False, notes=f"{type(e).__name__}: {e}")
            out.append(r)
            if i % 20 == 0 or r.get("found"):
                print(f"  [{i}/{len(todo)}] {r['slug']:22s} "
                      f"{'OK ' + str(r.get('cds_year') or '?') if r.get('found') else 'miss'}")
    json.dump(out, open("/tmp/cds/crawl.json", "w"), indent=1)
    hit = sum(1 for r in out if r.get("found"))
    print(f"\ncrawl: {hit}/{len(out)} found, with C7 for "
          f"{sum(1 for r in out if r.get('c7'))}")


if __name__ == "__main__":
    main()
