#!/usr/bin/env python3
"""Read a 2025-26-style CDS PDF from its AcroForm fields (no text layer needed).

Many CDS PDFs (UCLA, Wake Forest, Syracuse, UW, Bowdoin, Colgate...) keep every
answer in form fields and render nothing into the text layer, so cds_read.py
sees an empty document. The field names are the CDS template's own:

  C1   AP_RECD_1ST_{MEN,WMN,UNK}_N / AP_ADMT_1ST_{MEN,WMN,UNK}_N
  C9   SAT1_COMP_25TH_P / SAT1_COMP_75TH_P / ACT_COMP_25TH_P / ACT_COMP_75TH_P
  C21  AP_RECD_EDEC_N / AP_ADMT_EDEC_N
  C7   Q111_1..6 (academic) and Q112_1..13 (nonacademic; _10 race/ethnicity is
       gone post-SFFA), values /VI /I /C /NC. Mapping validated 2026-09-25: it
       reproduces Bowdoin, Swarthmore and UCLA's hand-verified grids exactly.

Usage: python3 scripts/cds_acroform.py <file.pdf> [...]
"""
import json, sys
from pypdf import PdfReader

ACAD = ["rigor_of_record", "class_rank", "gpa", "test_scores", "essay", "recommendations"]
NONACAD = {1: "interview", 2: "extracurriculars", 3: "talent_ability", 4: "character",
           5: "first_generation", 6: "legacy", 7: "geographical_residence",
           8: "state_residency", 9: "religious_affiliation", 11: "volunteer_work",
           12: "work_experience", 13: "level_of_interest"}
LEVEL = {"/VI": "very_important", "/I": "important", "/C": "considered", "/NC": "not_considered"}


def _n(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def read(path):
    fl = PdfReader(path).get_fields() or {}
    g = lambda k: (fl.get(k) or {}).get("/V")
    out = {"fields": len(fl)}
    ap = sum(_n(g(f"AP_RECD_1ST_{s}_N")) or 0 for s in ("MEN", "WMN", "UNK"))
    ad = sum(_n(g(f"AP_ADMT_1ST_{s}_N")) or 0 for s in ("MEN", "WMN", "UNK"))
    if ap and ad:
        out.update(applicants=ap, admitted=ad, accept=round(ad / ap, 4))
    for k, f in (("sat_25", "SAT1_COMP_25TH_P"), ("sat_75", "SAT1_COMP_75TH_P"),
                 ("act_25", "ACT_COMP_25TH_P"), ("act_75", "ACT_COMP_75TH_P")):
        if _n(g(f)):
            out[k] = _n(g(f))
    ea, ed = _n(g("AP_RECD_EDEC_N")), _n(g("AP_ADMT_EDEC_N"))
    if ea and ed:
        out.update(ed_applicants=ea, ed_admitted=ed, ed_rate=round(ed / ea, 4))
    c7 = {}
    for i, k in enumerate(ACAD, 1):
        v = str(g(f"Q111_{i}"))
        if v in LEVEL:
            c7[k] = LEVEL[v]
    for i, k in NONACAD.items():
        v = str(g(f"Q112_{i}"))
        if v in LEVEL:
            c7[k] = LEVEL[v]
    if c7:
        out["c7"] = c7
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, json.dumps(read(p)))
