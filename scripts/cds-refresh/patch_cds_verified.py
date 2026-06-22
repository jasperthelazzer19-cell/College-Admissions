"""Merge scrape results into CDS_VERIFIED dict in app.py.

Rules:
  - For slugs not in CDS_VERIFIED: ADD if confidence != low AND we have at least an accept rate.
  - For slugs already in CDS_VERIFIED: UPDATE only if our cds_year >= '2025-26' (strictly fresher).
  - Per-field guardrails (skip fields that fail bounds): accept 0.005..0.999, sat 800..1600, act 10..36.
  - Partial entries are OK — the dict consumer uses 'if k in cds and cds[k] is not None'.
"""
import json
import re
from pathlib import Path

APP_PY = Path("/Users/jasperlasser/college-tool/app.py")
RESULTS = Path("/Users/jasperlasser/college-tool/scripts/cds-refresh/results.jsonl")


def good_fields(e: dict) -> dict:
    f = {}
    a = e.get("accept")
    if a is not None and 0.005 <= a <= 0.999:
        f["accept"] = a
    for k, lo, hi in [("sat_25", 800, 1600), ("sat_75", 800, 1600), ("act_25", 10, 36), ("act_75", 10, 36)]:
        v = e.get(k)
        if v is not None and lo <= v <= hi:
            f[k] = v
    return f


def main():
    text = APP_PY.read_text()
    m = re.search(r"^CDS_VERIFIED = \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if not m:
        raise SystemExit("CDS_VERIFIED dict not found")
    existing = eval(m.group().split("=", 1)[1], {"__builtins__": {}}, {})
    print(f"existing CDS_VERIFIED: {len(existing)} entries")

    rows = [json.loads(l) for l in open(RESULTS)]
    new_entries: dict[str, dict] = {}
    updates: dict[str, dict] = {}

    for r in rows:
        if r.get("status") != "ok":
            continue
        slug = r["slug"]
        e = r.get("extracted") or {}
        if (e.get("confidence") or "low") == "low":
            continue
        fields = good_fields(e)
        if not fields:
            continue
        if slug in existing:
            if (e.get("cds_year") or "") >= "2025-26":
                # Merge: keep existing fields, overwrite with our newer ones
                merged = dict(existing[slug])
                merged.update(fields)
                if merged != existing[slug]:
                    updates[slug] = merged
        else:
            if "accept" in fields:
                new_entries[slug] = fields

    print(f"adds:    {len(new_entries)}")
    print(f"updates (2025-26 fresher): {len(updates)}")

    # Build the new dict body, preserving the original block's tail comment if any
    # We'll just produce a clean reformatted block.
    merged = dict(existing)
    merged.update(new_entries)
    merged.update(updates)
    print(f"final CDS_VERIFIED: {len(merged)} entries")

    # Render the new block. Format: one entry per line, sorted by slug.
    def fmt_v(v):
        if isinstance(v, float):
            # Preserve sensible precision
            return f"{v}"
        return str(v)

    lines = ["CDS_VERIFIED = {"]
    # Group: existing first (preserving order), then new entries (sorted)
    order = list(existing.keys()) + sorted(new_entries.keys())
    seen = set()
    for slug in order:
        if slug in seen:
            continue
        seen.add(slug)
        v = merged[slug]
        # Use compact dict-literal form on one line
        items = ", ".join(f'"{k}": {fmt_v(v[k])}' for k in ["accept", "sat_25", "sat_75", "act_25", "act_75"] if k in v)
        lines.append(f'    "{slug}": {{{items}}},')
    lines.append("}")
    new_block = "\n".join(lines)

    new_text = text[:m.start()] + new_block + text[m.end():]
    APP_PY.write_text(new_text)
    print(f"app.py updated: {len(merged)} entries written")


if __name__ == "__main__":
    main()
