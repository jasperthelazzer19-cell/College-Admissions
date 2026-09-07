#!/usr/bin/env python3
"""Read a whole CDS document deterministically: C1 counts, C9 score bands, C7 grid.

The agents' only irreplaceable job is FINDING the document. Once we have a URL,
everything in it can be pulled by code — no model, no search quota, no guessing.
C7 comes from cds_c7_extract (checkbox geometry); C1/C9 come from anchored
regexes over the page text, each one range-checked before it's returned.

Usage: python3 scripts/cds_read.py <url-or-path> [--json]
"""
import io, json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.cds_c7_extract import ROWS, extract as c7_extract, _fetch

# PDF text extraction sometimes injects spaces INSIDE a number ("1 0,300" for
# 10,300, "1 69" for 169), so allow interior spaces here and strip them in _first.
NUM = r"([\d][\d,\s]{1,12}\d|\d)"


def _pages(src):
    import pdfplumber
    src = _fetch(src)
    if isinstance(src, str):
        src = open(src, "rb")
    # Cap the page walk. Everything we need (C1/C7/C9) lives in the first
    # ~25 pages of a CDS, and a handful of schools publish 100+ page scanned
    # PDFs that take minutes to extract in full and stall the whole run.
    with pdfplumber.open(src) as pdf:
        return [p.extract_text() or "" for p in pdf.pages[:28]]


def _first(txt, pats, cast=int, lo=None, hi=None):
    for p in pats:
        for m in re.finditer(p, txt, re.I):
            try:
                v = cast(re.sub(r"[,\s]", "", m.group(1)))
            except Exception:
                continue
            if (lo is None or v >= lo) and (hi is None or v <= hi):
                return v
    return None


def _read_xlsx(src):
    """Read a CDS published as Excel.

    A growing number of schools (Cornell, Kenyon, others) publish the CDS as an
    .xlsx answer sheet instead of a PDF. That's a gift: the C7 importances are
    plain strings ("Very Important") sitting next to their row label, so there
    is no checkbox geometry to infer and no column to lose. Match on the label
    text rather than the C.7xx codes, since the codes vary by template year.
    """
    import openpyxl
    src = _fetch(src)
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    lvl = {"very important": "very_important", "important": "important",
           "considered": "considered", "not considered": "not_considered"}
    rec, c7 = {"found": True}, {}
    rows = []
    for sn in wb.sheetnames:
        for row in wb[sn].iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None]
            if cells:
                rows.append(cells)
    for cells in rows:
        low = [c.lower() for c in cells]
        for pat, key in ROWS:
            if key in c7:
                continue
            for i, c in enumerate(low):
                if re.search(pat, c):
                    for nxt in low[i + 1:i + 4]:
                        if nxt in lvl:
                            c7[key] = lvl[nxt]
                            break
                    break
        joined = " ".join(low)
        # The answer sheet carries stable machine field names — use them rather
        # than scanning a row for "the biggest number", which picked up a
        # gender-split row and reported Cornell as 36,834 applicants instead of
        # 72,523 (a 7.6% accept rate instead of the true 8.4%).
        for name, field in (("c1_total_first_time_first_years_who_applied_total", "applicants"),
                            ("c1_total_first_time_first_years_who_were_admitted_total", "admitted"),
                            ("c1_total_first_time_first_year_who_applied_total", "applicants"),
                            ("c1_total_first_time_first_year_who_were_admitted_total", "admitted")):
            if name in low:
                idx = low.index(name)
                for c in cells[idx + 1:]:
                    if re.fullmatch(r"[\d,\s]{3,12}", c or ""):
                        rec[field] = int(re.sub(r"[,\s]", "", c))
                        break
        if "name of college/university" in joined:
            idx = next((i for i, c in enumerate(low) if "name of college/university" in c), None)
            if idx is not None:
                for c in cells[idx + 1:]:
                    if len(c) > 4 and not re.fullmatch(r"[A-Z]\.\d+", c):
                        rec["institution"] = c
                        break
    # Grid-style workbooks (Berkeley) have no answer sheet: they reproduce the
    # printed CDS, so C7 is a row label plus an "x" sitting in one of four
    # columns, and C1 is a label with the total in the next cell. Column index
    # replaces the PDF's x-coordinate — same idea, exact instead of inferred.
    if len(c7) < 12:
        hdr = None
        for cells in rows:
            low = [c.lower() for c in cells]
            if {"very important", "important", "considered", "not considered"} <= set(low):
                hdr = {low.index("very important"): "very_important",
                       low.index("important"): "important",
                       low.index("considered"): "considered",
                       low.index("not considered"): "not_considered"}
                continue
            if not hdr:
                continue
            key = next((k for pat, k in ROWS if cells and re.search(pat, cells[0].lower())), None)
            if not key or key in c7:
                continue
            for i, c in enumerate(cells):
                if c.strip().lower() in ("x", "✓", "☒") and i in hdr:
                    c7[key] = hdr[i]
                    break
    if not rec.get("applicants"):
        for cells in rows:
            low = [c.lower() for c in cells]
            for i, c in enumerate(low):
                if re.match(r"total first[- ]time,? first[- ]year \(degree[- ]seeking\) who applied", c):
                    for nx in cells[i + 1:]:
                        if re.fullmatch(r"[\d,\s.]{3,12}", nx or ""):
                            rec["applicants"] = int(float(re.sub(r"[,\s]", "", nx))); break
                elif re.match(r"total first[- ]time,? first[- ]year \(degree[- ]seeking\) who were admitted", c):
                    for nx in cells[i + 1:]:
                        if re.fullmatch(r"[\d,\s.]{3,12}", nx or ""):
                            rec["admitted"] = int(float(re.sub(r"[,\s]", "", nx))); break
    if len(c7) >= 12 and "c7" not in rec:
        rec["c7"] = c7
    if rec.get("applicants") and rec.get("admitted") and rec["admitted"] <= rec["applicants"]:
        rec["accept"] = round(rec["admitted"] / rec["applicants"], 4)
    if len(c7) >= 12:
        rec["c7"] = c7
    return rec


def read(src):
    if isinstance(src, str) and src.lower().split("?")[0].endswith((".xlsx", ".xlsm", ".xls")):
        rec = _read_xlsx(src)
        m = re.search(r"(20\d\d)\s*[-_]\s*(20)?(\d\d)", src.rsplit("/", 1)[-1])
        if m:
            rec["cds_year"] = f"{m.group(1)}-20{m.group(3)}"
        return rec
    pages = _pages(src)
    txt = "\n".join(pages)
    if "Common Data Set" not in txt and "Relative importance" not in txt:
        return {"found": False, "notes": "not a CDS document"}
    rec = {"found": True}

    # ── C1: applicants / admitted. The CDS lists a gender breakdown FIRST
    # (males / females / unknown sex) and the grand total LAST, on a
    # '(degree-seeking)' row. A naive regex grabs the males row and reports
    # Harvard as 968/24000 instead of 2003/47893 — so match the total row
    # explicitly, and only fall back to summing the gender rows if it's absent.
    def total_row(verb):
        """The grand total for a C1 verb, across the layouts schools actually use.

        Three real variants, all seen in the wild:
          Harvard  'Total first-time, first-year (degree-seeking) who applied 47893'
          BC       'Total first-time, first-year who applied 34779 5401 25661 ...'
                   (total first, then a residency breakdown on the same line)
          Yale     no total row at all — only 'men who applied' / 'women who applied'
        So: try a total row with the parenthetical optional and take the FIRST
        number on the line (later numbers are the residency columns), and fall
        back to summing the gender rows. Gender wording varies too: men/women,
        males/females, 'students of unknown sex', 'another gender'.
        """
        paren = r"(?:\((?:degree[- ]seeking|freshman|first[- ]year)[^)]*\)\s*)?"
        pat = (r"Total first[- ]time,?\s*first[- ]year\s*" + paren +
               r"(?:students?\s*)?who\s*" + verb + r":?[^\d\n]*" + NUM)
        v = _first(txt, [pat], lo=10, hi=500000)
        if v is not None:
            return v
        parts = []
        for sex in ("men", "women", "males", "females",
                    r"students of unknown sex", r"another gender"):
            m = _first(txt, [r"Total first[- ]time,?\s*first[- ]year\s*" + paren + sex +
                             r"\s*who\s*" + verb + r":?[^\d\n]*" + NUM], lo=0, hi=400000)
            if m is not None:
                parts.append(m)
        return sum(parts) if len(parts) >= 2 else None

    ap = total_row(r"applied")
    ad = total_row(r"were admitted")
    if ap and ad and 0 < ad <= ap:
        rec.update(applicants=ap, admitted=ad, accept=round(ad / ap, 4))

    # ── C9: score bands. Rows read "<label> <25th> <50th> <75th>" or
    # "<label> <25th> <75th>"; take the first two plausible numbers and, when a
    # third is present and ordered, treat the middle one as the 50th.
    def band(label, lo, hi):
        for m in re.finditer(label + r"[^\n\d]{0,40}" + r"(\d{2,4})\s+(\d{2,4})(?:\s+(\d{2,4}))?", txt, re.I):
            g = [int(x) for x in m.groups() if x]
            g = [v for v in g if lo <= v <= hi]
            if len(g) >= 3 and g[0] <= g[1] <= g[2]:
                return g[0], g[2], g[1]
            if len(g) >= 2 and g[0] <= g[1]:
                return g[0], g[1], None
        return None
    b = band(r"SAT Composite", 400, 1600)
    if b:
        rec["sat_25"], rec["sat_75"] = b[0], b[1]
    b = band(r"ACT Composite", 1, 36)
    if b:
        rec["act_25"], rec["act_75"] = b[0], b[1]
        if b[2]:
            rec["act_50"] = b[2]
    b = band(r"SAT Evidence[- ]Based Reading and Writing", 100, 800)
    if b:
        rec["sat_erw_25"], rec["sat_erw_75"] = b[0], b[1]
    b = band(r"SAT Math", 100, 800)
    if b:
        rec["sat_math_25"], rec["sat_math_75"] = b[0], b[1]

    # ── C7 by checkbox geometry.
    try:
        c7, warns = c7_extract(src)
        if len(c7) >= 12:
            rec["c7"] = c7
        if warns:
            rec["c7_warnings"] = warns[:4]
    except Exception as e:
        rec["c7_error"] = f"{type(e).__name__}: {e}"

    # Year from the document text, else from the file name — plenty of CDS PDFs
    # never spell the year out in extractable text but carry it in the URL.
    # Institution name, from CDS section A1. Without this a search for "Pomona
    # common data set" happily returns CAL POLY POMONA's CDS and every number in
    # it parses perfectly — a wrong-school error that no range check can catch.
    for pat in (r"A1\s*[.:]?\s*Address Information[^\n]*\n\s*A1\s*Name of College/University[:\s]*([^\n]{3,80})",
                r"Name of College\s*/\s*University[:\s]*([^\n]{3,80})",
                r"Name of College/University[:\s]*([^\n]{3,80})"):
        mm = re.search(pat, txt, re.I)
        if mm:
            val = mm.group(1).strip(" :\t")
            # Some layouts put the next FORM LABEL where the name should be, so
            # "Street Address" got stored as Bates/Bowdoin/Colgate's institution
            # and then failed the identity check against their real names.
            if re.match(r"(?i)(street|address|city|state|zip|mailing|name of)\b", val):
                continue
            if len(val) > 3:
                rec["institution"] = val
                break

    m = re.search(r"Common Data Set\s*[:\s]\s*(20\d\d)\s*[-–—/]\s*(20)?(\d\d)", txt)
    if not m and isinstance(src, str):
        m = re.search(r"(20\d\d)\s*[-_–]\s*(20)?(\d\d)", src.rsplit("/", 1)[-1])
    if m:
        rec["cds_year"] = f"{m.group(1)}-20{m.group(3)}"
    return rec


if __name__ == "__main__":
    r = read(sys.argv[1])
    print(json.dumps(r, indent=1))
