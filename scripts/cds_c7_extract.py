#!/usr/bin/env python3
"""Deterministic CDS Section-C7 extractor.

Why this exists: `extract_text()` on a CDS PDF renders the C7 table as a row
label followed by a bare 'X' with NO column information — every importance level
collapses to the same string. An LLM reading that text cannot tell
'very_important' from 'not_considered' and will quietly guess. (It guessed
'considered' for all 18 of Harvard's factors, which is wrong on 6 of them.)

So we read WORD COORDINATES instead: find the x-centre of each of the four
column headers, find each factor's row by its y-centre, then assign the row's X
mark to the nearest column header. No inference, no model.

Usage:  python3 scripts/cds_c7_extract.py <file.pdf|url> [--json]
"""
import json, re, sys, io, urllib.request

LEVELS = [("Very Important", "very_important"), ("Important", "important"),
          ("Considered", "considered"), ("Not Considered", "not_considered")]

# CDS row label (as printed) -> Candor factor key.
ROWS = [
    (r"rigor of secondary school record", "rigor_of_record"),
    (r"class rank", "class_rank"),
    (r"academic gpa", "gpa"),
    (r"standardized test scores", "test_scores"),
    (r"application essay", "essay"),
    (r"recommendation", "recommendations"),
    (r"^interview", "interview"),
    (r"extracurricular", "extracurriculars"),
    (r"talent/ability|talent / ability", "talent_ability"),
    (r"character/personal|character / personal", "character"),
    (r"first generation|first-generation", "first_generation"),
    (r"alumni/ae relation|alumni relation|legacy", "legacy"),
    (r"geographical residence", "geographical_residence"),
    (r"state residency", "state_residency"),
    (r"religious affiliation", "religious_affiliation"),
    (r"volunteer work", "volunteer_work"),
    (r"work experience", "work_experience"),
    (r"level of applicant|level of interest", "level_of_interest"),
]


_CACHE = "/tmp/cds/cache"


def _fetch(src):
    """Fetch a PDF, caching to disk. The verify pass re-reads the same source
    URLs on every run; without a cache that re-downloads hundreds of PDFs."""
    if not isinstance(src, str) or not src.startswith("http"):
        return src
    import hashlib, os
    key = os.path.join(_CACHE, hashlib.sha1(src.encode()).hexdigest() + ".pdf")
    if os.path.exists(key) and os.path.getsize(key) > 0:
        return io.BytesIO(open(key, "rb").read())
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read()
    os.makedirs(_CACHE, exist_ok=True)
    open(key, "wb").write(data)
    return io.BytesIO(data)


def _col_centres(words):
    """x-centres of the four importance columns, from the header row.

    'Not Considered' and 'Considered' both contain the token 'Considered', and
    'Very Important' / 'Important' both contain 'Important' — so anchor on the
    qualifier words ('Very', 'Not') and treat a bare token as the plain column
    only when no qualifier sits immediately to its left on the same line.
    """
    cents = {}
    for w in words:
        t = w["text"].strip()
        if t not in ("Very", "Not", "Important", "Considered"):
            continue
        y = (w["top"] + w["bottom"]) / 2
        # The qualifier may sit to the LEFT on the same line ("Not Considered")
        # or DIRECTLY ABOVE it when the header wraps onto two lines, which is how
        # Duke and others render it. Accept both, or the column is never found
        # and the whole grid silently returns empty.
        left = [v for v in words if v["text"].strip() in ("Very", "Not")
                and abs((v["top"] + v["bottom"]) / 2 - y) < 4
                and 0 < w["x0"] - v["x1"] < 14]
        if not left:
            left = [v for v in words if v["text"].strip() in ("Very", "Not")
                    and 0 < y - (v["top"] + v["bottom"]) / 2 < 16
                    and not (v["x1"] < w["x0"] - 4 or v["x0"] > w["x1"] + 4)]
        if t in ("Very", "Not"):
            continue
        key = ({"Very": "very_important", "Not": "not_considered"}[left[0]["text"].strip()]
               if left else ("important" if t == "Important" else "considered"))
        # header spans the qualifier + the noun; centre on the pair when present
        x0 = left[0]["x0"] if left else w["x0"]
        cents.setdefault(key, []).append((x0 + w["x1"]) / 2)
    out = {k: sum(v) / len(v) for k, v in cents.items()}
    if len(out) >= 4:
        return out
    # Positional fallback. Duke renders the header as four separate tokens —
    # "Very | Important | Considered | Not" — with gaps too wide for the
    # adjacency pairing above, so only 2 columns resolve and the grid comes back
    # empty. When a single header line carries these tokens, trust their left-to
    # -right order: very_important, important, considered, not_considered.
    rows = {}
    for w in words:
        if w["text"].strip() in ("Very", "Important", "Considered", "Not"):
            rows.setdefault(round((w["top"] + w["bottom"]) / 2 / 3), []).append(w)
    for _band, ws in sorted(rows.items()):
        ws = sorted(ws, key=lambda v: v["x0"])
        seq = [v["text"].strip() for v in ws]
        if seq == ["Very", "Important", "Considered", "Not"]:
            keys = ["very_important", "important", "considered", "not_considered"]
            return {k: (v["x0"] + v["x1"]) / 2 for k, v in zip(keys, ws)}
    return out


def _widget_marks(page):
    """Checked checkboxes of a fillable CDS, as pseudo-words with x0/x1/top/bottom.

    A fillable PDF's text layer renders every C7 cell as an empty box, so the
    answers are invisible to text extraction. They live on the widget
    annotations instead: a checkbox is ON when its appearance state (/AS) is
    anything other than /Off. Return those as word-shaped dicts so the same
    column-snapping logic works unchanged.
    """
    out = []
    for a in (page.annots or []):
        if (a.get("data", {}) or {}).get("Subtype") and str(a["data"].get("Subtype")) != "/Widget":
            continue
        state = a.get("data", {}).get("AS")
        state = str(getattr(state, "name", state) or "")
        if not state or state.lstrip("/").lower() in ("off", ""):
            continue
        out.append({"text": "X", "x0": a["x0"], "x1": a["x1"],
                    "top": a["top"], "bottom": a["bottom"]})
    return out


def extract(src):
    import pdfplumber
    out, dbg = {}, []
    with pdfplumber.open(_fetch(src)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if "Relative importance" not in txt:
                continue
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            cols = _col_centres(words)
            if len(cols) < 3:
                continue
            # Three ways a CDS marks C7, measured over 70 real PDFs: a literal
            # X (34), a fillable AcroForm whose text layer shows only empty
            # boxes (13), and a dingbat check glyph (2). Take them in that order.
            marks = [w for w in words if w["text"].strip().upper() in ("X", "☒", "◼", "■")]
            if not marks:
                marks = [w for w in words if w["text"].strip() in ("✓", "✔", "●", "▪", "▪")]
            if not marks:
                marks = _widget_marks(page)
            lines = {}
            for w in words:
                lines.setdefault(round((w["top"] + w["bottom"]) / 2 / 3), []).append(w)
            for band, ws in sorted(lines.items()):
                label = " ".join(w["text"] for w in sorted(ws, key=lambda v: v["x0"])).lower()
                key = next((k for pat, k in ROWS if re.search(pat, label)), None)
                if not key or key in out:
                    continue
                y = band * 3
                row_marks = [m for m in marks if abs((m["top"] + m["bottom"]) / 2 - y) < 6]
                if len(row_marks) != 1:
                    dbg.append(f"{key}: {len(row_marks)} marks on row — skipped")
                    continue
                mx = (row_marks[0]["x0"] + row_marks[0]["x1"]) / 2
                lvl = min(cols, key=lambda k: abs(cols[k] - mx))
                if abs(cols[lvl] - mx) > 60:
                    dbg.append(f"{key}: mark {mx:.0f} far from any column — skipped")
                    continue
                out[key] = lvl
            if out:
                break
    return out, dbg


if __name__ == "__main__":
    src = sys.argv[1]
    grid, dbg = extract(src)
    if "--json" in sys.argv:
        print(json.dumps({"c7": grid, "n": len(grid), "warnings": dbg}, indent=1))
    else:
        for pat, k in ROWS:
            print(f"  {k:26s} {grid.get(k, '— NOT READ')}")
        print(f"\n{len(grid)}/18 factors read")
        for d in dbg:
            print("  warn:", d)
