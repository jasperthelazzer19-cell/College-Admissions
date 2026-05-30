"""Apply CDS-scrape updates to COLLEGES in app.py and school_stats_overrides table.

Guardrails (skip a field if any fails):
  - confidence == 'low'                 -> skip entire school
  - accept    : not in (0.005, 0.999)
  - sat_25/75 : not in (800, 1600)      catches EBRW-only bug
  - act_25/75 : not in (10, 36)
  - size      : < 500                   catches entering-class-only
  - tuition   : skip on type=='public'  (in-state vs OOS mismatch)
  - any field: relative change > 50% → keep current (definition-mismatch suspect)

Outputs:
  patched.csv    — every (slug, field) that got swapped: old/new/source_url
  skipped.csv    — every (slug, field) that was filtered out + reason
  app.py         — modified in place
  college.db     — modified in place (school_stats_overrides)
"""
from __future__ import annotations
import csv
import json
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
DB = HERE.parent.parent / "college.db"
RESULTS = HERE / "results.jsonl"

PATCHED_CSV = HERE / "patched.csv"
SKIPPED_CSV = HERE / "skipped.csv"

PATCH_FIELDS = ["accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"]


def safe_value(slug: str, college_type: str, field: str, current, new, conf: str) -> tuple[bool, str]:
    """Return (apply?, reason). Reason explains why skipped."""
    if conf == "low":
        return False, "low_confidence"
    if new is None:
        return False, "null_new"
    if current is None:
        return True, "ok_new_only"
    if abs(new - current) < 1e-6:
        return False, "no_change"
    # field-specific sanity bounds
    if field == "accept":
        if not (0.005 <= new <= 0.999):
            return False, f"accept_oor:{new}"
    elif field in ("sat_25", "sat_75"):
        if not (800 <= new <= 1600):
            return False, f"sat_oor:{new}"  # EBRW-only bug
    elif field in ("act_25", "act_75"):
        if not (10 <= new <= 36):
            return False, f"act_oor:{new}"
    elif field == "size":
        if new < 500:
            return False, f"size_too_small:{new}"  # entering-class bug
    elif field == "tuition":
        if college_type == "public":
            return False, "public_tuition_skipped"  # in-state vs OOS confusion
        if not (1000 <= new <= 100000):
            return False, f"tuition_oor:{new}"
    # relative-change guard (definition mismatch)
    if isinstance(current, (int, float)) and current:
        pct = abs(new - current) / current
        if pct > 0.5:
            return False, f"big_change:{pct:.1%}"
    return True, "ok"


def build_patch():
    rows = [json.loads(l) for l in open(RESULTS) if l.strip()]
    # Need college metadata (type) to apply tuition guard — load from app.py
    text = APP_PY.read_text()
    start = text.find("COLLEGES = [")
    end = text.find("\nCOLLEGES_BY_SLUG")
    block = text[start:end].split("=", 1)[1].strip().rstrip(",")
    colleges = eval(block, {"__builtins__": {}}, {})
    by_slug = {c["slug"]: c for c in colleges}

    patched = []   # rows: slug,name,field,old,new,source_url,year
    skipped = []   # rows: slug,name,field,old,new,reason

    for r in rows:
        if r.get("status") != "ok":
            continue
        slug = r["slug"]
        ext = r.get("extracted") or {}
        conf = ext.get("confidence") or "low"
        src = ext.get("source_url") or ""
        year = ext.get("cds_year") or ""
        current = by_slug.get(slug, {})
        ctype = current.get("type", "")
        for field in PATCH_FIELDS:
            cur_v = current.get(field)
            new_v = ext.get(field)
            apply, reason = safe_value(slug, ctype, field, cur_v, new_v, conf)
            base = {
                "slug": slug, "name": r["name"], "field": field,
                "old": cur_v, "new": new_v, "source_url": src,
                "year": year, "confidence": conf,
            }
            if apply:
                patched.append(base)
            else:
                skipped.append({**base, "reason": reason})
    return patched, skipped, by_slug


def write_csvs(patched, skipped):
    with open(PATCHED_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["slug","name","field","old","new","year","confidence","source_url"])
        w.writeheader()
        for row in patched:
            w.writerow({k: row[k] for k in w.fieldnames})
    with open(SKIPPED_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["slug","name","field","old","new","reason","year","confidence","source_url"])
        w.writeheader()
        for row in skipped:
            w.writerow({k: row[k] for k in w.fieldnames})
    print(f"wrote {PATCHED_CSV.name} ({len(patched)} edits) and {SKIPPED_CSV.name} ({len(skipped)} skipped)")


def patch_app_py(patched):
    """For each (slug, field, new), find the slug's line in app.py and replace the field value."""
    text = APP_PY.read_text()
    # Group patches by slug
    by_slug: dict[str, dict[str, object]] = {}
    for p in patched:
        by_slug.setdefault(p["slug"], {})[p["field"]] = p["new"]

    new_text = text
    fixed = 0
    misses = []
    for slug, updates in by_slug.items():
        # Find the dict literal line for this slug (one line per college)
        # Pattern: any line containing "slug":"<slug>" (allow whitespace variations)
        pat = re.compile(r'^(.*"slug":"' + re.escape(slug) + r'".*)$', re.MULTILINE)
        m = pat.search(new_text)
        if not m:
            misses.append(slug)
            continue
        line = m.group(1)
        for field, new_val in updates.items():
            # Replace "field":<value> in this line. Value can be int or float.
            field_pat = re.compile(r'("' + re.escape(field) + r'":)([0-9.]+)')
            if isinstance(new_val, float):
                new_str = f"{new_val:.4g}".rstrip("0").rstrip(".") if isinstance(new_val, float) else str(new_val)
                # For accept, want decimal like 0.046 — preserve enough precision
                if field == "accept":
                    new_str = f"{new_val:.4f}".rstrip("0").rstrip(".")
            else:
                new_str = str(int(new_val))
            new_line, n = field_pat.subn(lambda m_: m_.group(1) + new_str, line, count=1)
            if n == 1:
                line = new_line
        new_text = new_text.replace(m.group(1), line, 1)
        fixed += 1

    APP_PY.write_text(new_text)
    print(f"patched app.py: {fixed} school lines updated, {len(misses)} not found")
    if misses:
        print(f"  misses: {misses[:10]}{'...' if len(misses)>10 else ''}")


def patch_db(patched):
    """INSERT into school_stats_overrides, one row per slug with all patched fields."""
    by_slug: dict[str, dict[str, object]] = {}
    for p in patched:
        by_slug.setdefault(p["slug"], {})[p["field"]] = p["new"]
    # Need source_url too
    src_by_slug: dict[str, str] = {}
    for p in patched:
        src_by_slug[p["slug"]] = p["source_url"]

    conn = sqlite3.connect(DB)
    n = 0
    for slug, vals in by_slug.items():
        conn.execute("""
            INSERT INTO school_stats_overrides
            (college_slug, accept, sat_25, sat_75, act_25, act_75, size, tuition, source, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(college_slug) DO UPDATE SET
              accept   = COALESCE(excluded.accept,   accept),
              sat_25   = COALESCE(excluded.sat_25,   sat_25),
              sat_75   = COALESCE(excluded.sat_75,   sat_75),
              act_25   = COALESCE(excluded.act_25,   act_25),
              act_75   = COALESCE(excluded.act_75,   act_75),
              size     = COALESCE(excluded.size,     size),
              tuition  = COALESCE(excluded.tuition,  tuition),
              source   = excluded.source,
              verified_at = CURRENT_TIMESTAMP
        """, (
            slug,
            vals.get("accept"), vals.get("sat_25"), vals.get("sat_75"),
            vals.get("act_25"), vals.get("act_75"),
            vals.get("size"), vals.get("tuition"),
            f"CDS scrape 2026-05-20 · {src_by_slug.get(slug,'')[:200]}",
        ))
        n += 1
    conn.commit()
    conn.close()
    print(f"patched college.db: {n} schools in school_stats_overrides")


def main():
    patched, skipped, _ = build_patch()
    write_csvs(patched, skipped)
    # Skip-reason breakdown
    from collections import Counter
    reasons = Counter(s["reason"] for s in skipped)
    print("\nskip reasons:")
    for r, n in reasons.most_common():
        print(f"  {r:<32} {n}")
    print()
    patch_app_py(patched)
    patch_db(patched)


if __name__ == "__main__":
    main()
