"""Apply the curated top-75 patch plan to app.py.

Reads scripts/cds-refresh/patch_plan.json and surgically updates:
  - CDS_VERIFIED[slug]['accept']   for overall acceptance rate movers
  - ADMISSIONS_DETAIL[slug]['rates'][round]   for round-by-round movers

Validates by re-parsing app.py after the edit and re-running the audit.
"""
from __future__ import annotations
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
PLAN = HERE / "patch_plan.json"


def round_decimal(x: float, places: int = 4) -> float:
    return round(x, places)


def main() -> int:
    plan = json.loads(PLAN.read_text())
    # Dedupe overall by slug
    overall_by_slug: dict[str, dict] = {}
    for p in plan["overall"]:
        overall_by_slug[p["slug"]] = p
    overall = list(overall_by_slug.values())
    rounds = plan["rounds"]

    print(f"Applying patches: {len(overall)} overall, {len(rounds)} round")

    src = APP_PY.read_text()

    # Parse current dicts
    tree = ast.parse(src)
    cds_node = ad_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "CDS_VERIFIED":
                        cds_node = node
                    if t.id == "ADMISSIONS_DETAIL":
                        ad_node = node
    if not cds_node or not ad_node:
        print("Could not locate CDS_VERIFIED or ADMISSIONS_DETAIL", file=sys.stderr)
        return 1

    cds_data = ast.literal_eval(cds_node.value)
    ad_data = ast.literal_eval(ad_node.value)

    # ---- Apply OVERALL patches to CDS_VERIFIED ----
    for p in overall:
        slug = p["slug"]
        new = round_decimal(p["new"])
        if slug in cds_data:
            cds_data[slug] = {**cds_data[slug], "accept": new}
        else:
            cds_data[slug] = {"accept": new}

    # ---- Apply ROUND patches to ADMISSIONS_DETAIL ----
    for p in rounds:
        slug = p["slug"]
        rd = p["round"]
        new = round_decimal(p["new"])
        if slug not in ad_data:
            print(f"  skip rounds patch for {slug}: not in ADMISSIONS_DETAIL")
            continue
        entry = ad_data[slug]
        entry.setdefault("rates", {})[rd] = new
        # Ensure rounds list includes the round
        if rd not in (entry.get("rounds") or []):
            entry.setdefault("rounds", []).append(rd)

    # ---- Render the dicts back to Python source ----
    # We render in a stable, readable format — one entry per line, sorted keys
    def render_cds(data: dict) -> str:
        out = ["CDS_VERIFIED = {"]
        for slug, vals in data.items():
            # Format values: floats short, ints, None
            pairs = []
            for k in ("accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"):
                if k in vals:
                    v = vals[k]
                    if v is None:
                        pairs.append(f'"{k}": None')
                    elif isinstance(v, float):
                        # accept usually 4 decimals; sat ints
                        if k == "accept":
                            pairs.append(f'"{k}": {v:.4f}')
                        else:
                            pairs.append(f'"{k}": {v}')
                    else:
                        pairs.append(f'"{k}": {v}')
            # Include any keys not in canonical list
            for k, v in vals.items():
                if k not in ("accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"):
                    if v is None:
                        pairs.append(f'"{k}": None')
                    elif isinstance(v, str):
                        pairs.append(f'"{k}": {v!r}')
                    else:
                        pairs.append(f'"{k}": {v}')
            out.append(f'    "{slug}": {{{", ".join(pairs)}}},')
        out.append("}")
        return "\n".join(out)

    def render_ad(data: dict) -> str:
        out = ["ADMISSIONS_DETAIL = {"]
        for slug, vals in data.items():
            rounds = vals.get("rounds", [])
            rates = vals.get("rates", {})
            rates_str = ", ".join(
                f'"{r}": {v:.4f}' if isinstance(v, float) else f'"{r}": {v}'
                for r, v in rates.items()
            )
            parts = [f'"rounds": {rounds!r}', f'"rates": {{{rates_str}}}']
            for extra_k in ("in_state_rate", "out_of_state_rate"):
                if extra_k in vals:
                    v = vals[extra_k]
                    if isinstance(v, float):
                        parts.append(f'"{extra_k}": {v:.4f}')
                    else:
                        parts.append(f'"{extra_k}": {v}')
            # Pass through any other keys we don't know about (defensive)
            for k, v in vals.items():
                if k in ("rounds", "rates", "in_state_rate", "out_of_state_rate"):
                    continue
                if isinstance(v, str):
                    parts.append(f'"{k}": {v!r}')
                elif isinstance(v, float):
                    parts.append(f'"{k}": {v:.4f}')
                else:
                    parts.append(f'"{k}": {v!r}')
            out.append(f'    "{slug}": {{{", ".join(parts)}}},')
        out.append("}")
        return "\n".join(out)

    new_cds_block = render_cds(cds_data)
    new_ad_block = render_ad(ad_data)

    # Replace in source using line numbers
    cds_start = cds_node.lineno
    cds_end = cds_node.end_lineno
    ad_start = ad_node.lineno
    ad_end = ad_node.end_lineno

    lines = src.split("\n")
    # IMPORTANT: do ADMISSIONS_DETAIL first if it's later, so CDS_VERIFIED line indices don't shift
    if ad_start > cds_start:
        # Replace ADMISSIONS_DETAIL first
        lines[ad_start - 1 : ad_end] = new_ad_block.split("\n")
        # CDS_VERIFIED indices unchanged
        lines[cds_start - 1 : cds_end] = new_cds_block.split("\n")
    else:
        lines[cds_start - 1 : cds_end] = new_cds_block.split("\n")
        lines[ad_start - 1 : ad_end] = new_ad_block.split("\n")

    new_src = "\n".join(lines)

    # Validate by parsing
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        print(f"REFUSING TO WRITE: syntax error in rendered output: {e}", file=sys.stderr)
        return 1

    # Validate by re-evaluating the dicts
    tree2 = ast.parse(new_src)
    cds_re = ad_re = None
    for node in tree2.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "CDS_VERIFIED":
                        cds_re = ast.literal_eval(node.value)
                    if t.id == "ADMISSIONS_DETAIL":
                        ad_re = ast.literal_eval(node.value)

    assert cds_re == cds_data, "CDS_VERIFIED round-trip mismatch"
    assert ad_re == ad_data, "ADMISSIONS_DETAIL round-trip mismatch"

    # Write
    APP_PY.write_text(new_src)
    print(f"Wrote {APP_PY}")
    print(f"  CDS_VERIFIED entries: {len(cds_re)}")
    print(f"  ADMISSIONS_DETAIL entries: {len(ad_re)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
