"""Better CDS PDF parser — detects whether school runs ED/EA, then extracts numbers carefully.

Strategy per school:
  1. Find C21 block, check if school OFFERS Early Decision (Yes/No)
     - If No: flag school as "no_ed_program" — its current ED rate is FAKE
     - If Yes: find "Number of early decision applications" label, capture immediate number
  2. Find C22 block, check if school OFFERS Early Action (Yes/No)
     - If No: flag "no_ea_program"
     - If Yes: try to extract EA app/admit numbers (often not broken out)
  3. Find C1 block, extract overall apps + admits (more careful regex)
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from pypdf import PdfReader

CACHE = Path("/tmp/cds_pdfs")


def extract_full_text(pdf_path: Path) -> str:
    try:
        rd = PdfReader(str(pdf_path))
        return "\n".join(p.extract_text() or "" for p in rd.pages)
    except Exception:
        return ""


def section_c21(text: str) -> str:
    # C21 starts; ends at C22, C23, C24, D., or "TRANSFER ADMISSION"
    m = re.search(r'C21[\s\S]*?(?=C22\b|C23\b|D\. |D1\. |TRANSFER)', text)
    return m.group(0) if m else ""


def section_c22(text: str) -> str:
    m = re.search(r'C22[\s\S]*?(?=C23\b|C24\b|D\. |D1\. |TRANSFER)', text)
    return m.group(0) if m else ""


def section_c1(text: str) -> str:
    m = re.search(r'C1[\s\S]*?(?=C3\b|C2\.\s)', text)
    return m.group(0) if m else ""


def ed_program_offered(c21: str) -> bool | None:
    """Look at the 'Yes No' marker for 'Does your institution offer an early decision plan'."""
    if not c21:
        return None
    # Pattern 1: "Yes\nNo\nX" or "Yes No\nX" — marker after the question
    # Pattern 2: Just "No" or "Yes" by itself shortly after the question
    intro = re.search(r'offer an early decision plan[\s\S]{0,400}', c21, re.IGNORECASE)
    if not intro:
        return None
    chunk = intro.group(0)
    # Look for explicit "No" answer first (since most schools answer right after the question)
    # Common patterns: "No\n\nFor the Fall..." or "Yes No\nX\n...12/5" (with X marking Yes)
    # If "X" appears near "Yes" → Yes; near "No" → No

    # Simple heuristic: find first "Yes" / "No" word after the question
    no_match = re.search(r'(?:^|\n)\s*No\s*(?:\n|$)', chunk[:300])
    yes_match = re.search(r'(?:^|\n)\s*Yes\s*(?:\n|$)', chunk[:300])
    x_match = re.search(r'(?:^|\n)\s*[Xx]\s*(?:\n|$)', chunk[:300])

    # If the chunk has "Yes No\nX" or "Yes\nNo\nX" — X = which one?
    # Look at "X" position relative to Yes/No
    if 'Yes No' in chunk[:200] or 'Yes\nNo' in chunk[:300]:
        # Find both Yes and No positions, then X
        yn = re.search(r'Yes\s*No', chunk[:300])
        if yn:
            after = chunk[yn.end():yn.end() + 100]
            # If "X" is in line immediately after, school answered Yes (X under Yes column)
            # But hard to tell from text extraction. Look for a date next — if there's a "11/1" or
            # similar date, ED is offered (those fields only populated when Yes)
            date_match = re.search(r'\b\d{1,2}/\d{1,2}\b', after)
            return bool(date_match)

    # Fallback: if "X" appears alone followed by "11/1" type date → yes
    if x_match and re.search(r'\b\d{1,2}/\d{1,2}\b', chunk):
        return True
    # If only "No" appears
    if no_match and not yes_match:
        return False
    # If "Click or tap here to enter text" is in C21 ED apps area → school left blank (sometimes ED offered, sometimes not)
    if 'Click or tap' in chunk and 'No' in chunk[:200]:
        return False
    return None


def extract_ed_numbers(c21: str) -> dict:
    """Find ED applications and admits in C21 — only if labels exist with numbers next to them."""
    out = {}
    # Look for the very specific labels followed by a number
    # Pattern: "Number of early decision applications received by your institution: 1053"
    # or just "Number of early decision applications received by your institution\n1053"

    # Apps
    m = re.search(
        r'Number of early decision applications received by your institution\s*[:.]?\s*\n*\s*(\d{1,2}(?:,\d{3})+|\d{2,5})',
        c21, re.IGNORECASE
    )
    if m:
        n = int(m.group(1).replace(',', ''))
        if 10 < n < 50_000:
            out['ed_apps'] = n

    # Admits
    m = re.search(
        r'Number of applicants admitted under early decision plan\s*[:.]?\s*\n*\s*(\d{1,2}(?:,\d{3})+|\d{2,5})',
        c21, re.IGNORECASE
    )
    if m:
        n = int(m.group(1).replace(',', ''))
        if 1 < n < 20_000:
            out['ed_admits'] = n

    if 'ed_apps' in out and 'ed_admits' in out:
        if out['ed_admits'] < out['ed_apps']:
            out['ed_rate'] = out['ed_admits'] / out['ed_apps']

    return out


def main():
    flagged = json.load(open('/tmp/flagged_urls.json'))['flagged']
    results = []

    for slug in flagged:
        pdf_path = CACHE / f"{slug}.pdf"
        rec = {'slug': slug}
        if not pdf_path.exists() or pdf_path.stat().st_size < 5000:
            rec['error'] = 'no_pdf'
            results.append(rec)
            continue
        text = extract_full_text(pdf_path)
        if len(text) < 500:
            rec['error'] = 'no_text'
            results.append(rec)
            continue
        c21 = section_c21(text)
        ed_offered = ed_program_offered(c21)
        rec['ed_offered'] = ed_offered
        if ed_offered:
            rec['ed'] = extract_ed_numbers(c21)
        results.append(rec)

    # Print summary
    print(f"{'school':16s} {'offers_ed':>10s}  ed_apps  ed_admits  ed_rate")
    print('-' * 70)
    for r in results:
        if r.get('error'):
            print(f"  {r['slug']:16s} ERROR: {r['error']}")
            continue
        ed = r.get('ed', {})
        rate = ed.get('ed_rate')
        rate_str = f"{rate*100:.2f}%" if rate else 'N/A'
        print(f"  {r['slug']:16s} {str(r.get('ed_offered')):>10s}  {str(ed.get('ed_apps')):>8s}  {str(ed.get('ed_admits')):>8s}  {rate_str}")

    Path('scripts/cds-refresh/cds_ed_parse.json').write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
