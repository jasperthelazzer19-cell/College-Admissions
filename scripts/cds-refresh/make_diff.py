"""Generate human-readable diff from results.jsonl.

Outputs:
  diff.csv        — every field change, one row per (slug,field) that changed
  summary.md      — top-level summary: counts, biggest movers, failures
"""
from __future__ import annotations
import csv
import json
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "results.jsonl"
DIFF_CSV = HERE / "diff.csv"
SUMMARY = HERE / "summary.md"

FIELDS = ["accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"]


def load() -> list[dict]:
    if not RESULTS.exists():
        raise SystemExit("results.jsonl not found — run scrape_cds.py first")
    return [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]


def fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.4f}" if abs(x) < 1 else f"{x:.2f}"
    return str(x)


def main():
    rows = load()
    ok = [r for r in rows if r.get("status") == "ok"]
    fail = [r for r in rows if r.get("status") != "ok"]

    diffs = []
    movers = []  # (slug, name, field, current, new, abs_delta, pct_delta)
    for r in ok:
        ext = r.get("extracted") or {}
        d = r.get("diff") or {}
        for f in FIELDS:
            entry = d.get(f, {})
            cur = entry.get("current")
            new = entry.get("new")
            delta = entry.get("delta")
            if delta in ("=", None):
                continue
            row = {
                "slug": r["slug"],
                "name": r["name"],
                "field": f,
                "current": fmt(cur),
                "new": fmt(new),
                "delta": delta,
                "cds_year": ext.get("cds_year"),
                "source_url": ext.get("source_url"),
                "confidence": ext.get("confidence"),
            }
            diffs.append(row)
            if cur is not None and new is not None and isinstance(cur, (int, float)):
                abs_d = abs(new - cur)
                pct = (abs_d / cur) * 100 if cur else 0
                movers.append((r["slug"], r["name"], f, cur, new, abs_d, pct))

    # diff CSV
    with open(DIFF_CSV, "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["slug","name","field","current","new","delta","cds_year","source_url","confidence"])
        w.writeheader()
        for row in sorted(diffs, key=lambda r: (r["slug"], r["field"])):
            w.writerow(row)

    # summary
    movers.sort(key=lambda t: t[6], reverse=True)
    lines = []
    lines.append(f"# Candor CDS refresh — {len(rows)} colleges")
    lines.append("")
    lines.append(f"- ok: **{len(ok)}**")
    lines.append(f"- failed: **{len(fail)}**")
    lines.append(f"- total field changes: **{len(diffs)}**")
    lines.append("")
    lines.append("## Biggest % movers (changes worth eyeballing)")
    lines.append("")
    lines.append("| College | Field | Current | New | % |")
    lines.append("|---|---|---|---|---|")
    for slug, name, f, cur, new, abs_d, pct in movers[:40]:
        lines.append(f"| {name} | {f} | {fmt(cur)} | {fmt(new)} | {pct:.1f}% |")
    lines.append("")
    lines.append("## Confidence breakdown")
    conf_counts = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    for r in ok:
        c = (r.get("extracted") or {}).get("confidence") or "unknown"
        conf_counts[c] = conf_counts.get(c, 0) + 1
    for k, v in conf_counts.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    if fail:
        lines.append("## Failures (need manual review)")
        lines.append("")
        for r in fail:
            err = (r.get("error") or "?")[:120].replace("\n", " ")
            lines.append(f"- **{r['name']}** ({r['slug']}): {r.get('status')} — {err}")

    SUMMARY.write_text("\n".join(lines))
    print(f"wrote {DIFF_CSV} ({len(diffs)} rows) and {SUMMARY}")
    print(f"  ok={len(ok)} failed={len(fail)}")
    if fail:
        print(f"  failures: {', '.join(r['slug'] for r in fail[:10])}{'...' if len(fail)>10 else ''}")


if __name__ == "__main__":
    main()
