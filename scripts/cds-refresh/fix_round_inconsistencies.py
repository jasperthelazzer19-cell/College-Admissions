"""Fix round-by-round inconsistencies by reading CDS PDFs directly.

For each flagged school:
  1. Use cached URL from prior scrapes. If it's already a PDF, use it.
     If HTML, fetch with crwl, find the newest CDS PDF link.
  2. Download PDF, extract ED apps + admits from C21 section.
  3. Optionally extract overall apps + admits from C1.
  4. Output a proposed patch.

Free — no API calls.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from pypdf import PdfReader

HERE = Path(__file__).parent
CACHE = Path("/tmp/cds_pdfs")
CACHE.mkdir(exist_ok=True)

# Pattern for CDS PDF link discovery
CDS_PDF_PATTERNS = [
    re.compile(r'(https?://[^\s"\'<>]+?(?:cds|common[-_]?data[-_]?set)[^\s"\'<>]*?\.pdf)', re.IGNORECASE),
    re.compile(r'(https?://[^\s"\'<>]+?\.pdf)(?=[^a-z]|$)', re.IGNORECASE),  # any pdf
]

YEAR_RE = re.compile(r'(20\d{2})[-_/](20\d{2}|\d{2})')


def is_pdf_url(url: str) -> bool:
    return url.lower().split('?')[0].endswith('.pdf')


def fetch_html(url: str) -> str | None:
    try:
        r = subprocess.run(
            ["crwl", "crawl", url, "-o", "md"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def find_cds_pdf_from_landing(landing_url: str) -> str | None:
    """Fetch a CDS landing HTML page, return URL of newest-year CDS PDF."""
    html = fetch_html(landing_url)
    if not html:
        return None
    # Find all PDF links
    pdf_links = set()
    for pat in CDS_PDF_PATTERNS:
        pdf_links.update(pat.findall(html))
    if not pdf_links:
        # Try relative paths
        rel_pdfs = re.findall(r'(["\'])(/[^"\']+\.pdf)\1', html)
        for _, rel in rel_pdfs:
            base = '/'.join(landing_url.split('/')[:3])
            pdf_links.add(base + rel)
    if not pdf_links:
        return None
    # Pick newest by year mentioned in URL or filename
    def score(url: str) -> tuple:
        years = YEAR_RE.findall(url)
        year_max = 0
        for y1, _ in years:
            year_max = max(year_max, int(y1))
        # Prefer "common-data-set" / "cds" in path
        is_cds = ('cds' in url.lower() or 'common' in url.lower())
        return (is_cds, year_max)
    return max(pdf_links, key=score)


def download_pdf(url: str, slug: str) -> Path | None:
    dest = CACHE / f"{slug}.pdf"
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    try:
        r = subprocess.run(
            ["curl", "-sL", "-o", str(dest), "--max-time", "30", url],
            capture_output=True, timeout=35,
        )
        if dest.exists() and dest.stat().st_size > 5_000:
            return dest
    except Exception:
        pass
    return None


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        rd = PdfReader(str(pdf_path))
        return "\n\n".join(p.extract_text() or "" for p in rd.pages)
    except Exception:
        return ""


def parse_ed_numbers(text: str) -> dict:
    """From CDS C21 section, extract ED applications & admits."""
    # Find C21 block
    m = re.search(r'C21[\s\S]{0,2500}', text)
    if not m:
        return {}
    block = m.group(0)
    # Common phrasing: "Number of early decision applications received by your institution"
    # Then admits: "Number of applicants admitted under early decision plan"
    apps = admits = None

    # Try canonical phrases first
    app_patterns = [
        r'Number of early decision applications[^\n]*\n?\s*(\d{1,2}[,\d]{2,})',
        r'received by your institution[\s\S]{0,200}?(\d{3,6})',
    ]
    adm_patterns = [
        r'Number of applicants admitted under early decision[^\n]*\n?\s*(\d{1,2}[,\d]{2,})',
        r'admitted under early decision plan[\s\S]{0,200}?(\d{2,5})',
    ]

    for p in app_patterns:
        m = re.search(p, block, re.IGNORECASE)
        if m:
            apps = int(m.group(1).replace(',', ''))
            break
    for p in adm_patterns:
        m = re.search(p, block, re.IGNORECASE)
        if m:
            admits = int(m.group(1).replace(',', ''))
            break

    # Fallback: look for two large-ish numbers in the C21 block
    if apps is None or admits is None:
        nums = [int(x.replace(',', '')) for x in re.findall(r'\b(\d{2,3}(?:,\d{3})*)\b', block)]
        plausible = [n for n in nums if 30 < n < 50_000]
        if len(plausible) >= 2:
            apps = apps or max(plausible[:8])
            others = [n for n in plausible if n != apps]
            if others:
                admits = admits or max([n for n in others if n < apps], default=None)

    out = {}
    if apps: out['ed_apps'] = apps
    if admits: out['ed_admits'] = admits
    if apps and admits and admits < apps:
        out['ed_rate'] = admits / apps
    return out


def parse_overall_numbers(text: str) -> dict:
    """From CDS C1 section, extract total apps & admits."""
    # The C1 block has totals
    m = re.search(r'C1[\s\S]{0,3000}', text)
    if not m:
        return {}
    block = m.group(0)
    # Common: pull all 4+ digit numbers; first big one is apps, second is admits
    nums = [int(x.replace(',', '')) for x in re.findall(r'\b(\d{1,3}(?:,\d{3})+|\d{4,6})\b', block)]
    plausible = [n for n in nums if 200 < n < 200_000]
    if len(plausible) >= 2:
        # First very large = apps, next smaller is admits
        apps = max(plausible[:6])
        below = [n for n in plausible if n < apps and n > 10]
        if below:
            admits = max(below)
            return {'apps': apps, 'admits': admits, 'rate': admits / apps}
    return {}


def process_school(slug: str, urls_for_slug: str) -> dict:
    rec = {'slug': slug, 'urls': urls_for_slug}
    # urls_for_slug can contain " and " separator
    candidates = re.split(r'\s+and\s+|\s*,\s*', urls_for_slug)
    candidates = [u for u in candidates if u.startswith('http')]

    pdf_url = None
    for u in candidates:
        if is_pdf_url(u):
            pdf_url = u
            break
    if not pdf_url:
        for u in candidates:
            try:
                cand = find_cds_pdf_from_landing(u)
                if cand:
                    pdf_url = cand
                    break
            except Exception as e:
                continue

    if not pdf_url:
        rec['error'] = 'no_pdf_found'
        return rec
    rec['pdf_url'] = pdf_url

    pdf_path = download_pdf(pdf_url, slug)
    if not pdf_path:
        rec['error'] = 'download_failed'
        return rec

    text = extract_pdf_text(pdf_path)
    if len(text) < 500:
        rec['error'] = 'text_extraction_failed'
        return rec

    rec['ed'] = parse_ed_numbers(text)
    rec['overall'] = parse_overall_numbers(text)
    return rec


def main():
    data = json.load(open('/tmp/flagged_urls.json'))
    urls = data['urls']
    flagged = data['flagged']

    results = []
    for slug in flagged:
        if slug not in urls:
            results.append({'slug': slug, 'error': 'no_url'})
            continue
        print(f'-> {slug}')
        rec = process_school(slug, urls[slug])
        print(f'   ed={rec.get("ed", {})}  overall={rec.get("overall", {})}  err={rec.get("error")}')
        results.append(rec)

    with open(HERE / 'cds_pdf_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f'\nWrote {HERE / "cds_pdf_results.json"}')


if __name__ == "__main__":
    main()
