#!/usr/bin/env python3
"""Write the verified CDS proposal into candor_data.py.

Two targets, matching the existing override hierarchy:
  CDS_VERIFIED[slug] = {accept, sat_25, sat_75, act_25, act_75}  (layer 1)
  C7_FACTORS[slug]   = {18 factors}                              (feeds odds + UI)

Only writes fields the proposal actually carries, never invents one, and leaves
any school absent from the proposal exactly as it was. --dry prints the diff and
changes nothing.

Usage: python3 scripts/cds_apply.py [--dry]
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DRY = "--dry" in sys.argv
PROP = json.load(open("/tmp/cds/proposal.json"))
SCORE_KEYS = ("accept", "sat_25", "sat_75", "act_25", "act_75")


def fmt_cds(slug, d):
    parts = [f'"{k}": {d[k]}' for k in SCORE_KEYS if d.get(k) is not None]
    return f'    "{slug}": {{{", ".join(parts)}}},'


def fmt_c7(slug, g):
    order = ["rigor_of_record", "class_rank", "gpa", "test_scores", "essay",
             "recommendations", "interview", "extracurriculars", "talent_ability",
             "character", "first_generation", "legacy", "geographical_residence",
             "state_residency", "religious_affiliation", "volunteer_work",
             "work_experience", "level_of_interest"]
    parts = [f'"{k}": "{g[k]}"' for k in order if k in g]
    return f'    "{slug}": {{{", ".join(parts)}}},'


def splice(src, dict_name, new_rows):
    """Replace/insert rows in a top-level dict literal, keeping everything else."""
    m = re.search(rf"^{dict_name} = \{{", src, re.M)
    if not m:
        raise SystemExit(f"{dict_name} not found")
    i = m.end()
    depth, j = 1, i
    while depth:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
        j += 1
    body = src[i:j - 1]
    kept, replaced = [], 0
    for line in body.split("\n"):
        km = re.match(r'\s*"([\w-]+)":', line)
        if km and km.group(1) in new_rows:
            kept.append(new_rows.pop(km.group(1)))
            replaced += 1
        else:
            kept.append(line)
    added = list(new_rows.values())
    if added:
        while kept and not kept[-1].strip():
            kept.pop()
        kept += added + [""]
    return src[:i] + "\n".join(kept) + src[j - 1:], replaced, len(added)


def main():
    path = os.path.join(ROOT, "candor_data.py")
    src = open(path).read()
    import candor_data as cd

    cds_rows, c7_rows, changed = {}, {}, []
    for slug, d in sorted(PROP.items()):
        vals = {k: d[k] for k in SCORE_KEYS if d.get(k) is not None}
        if vals:
            old = cd.CDS_VERIFIED.get(slug, {})
            if any(old.get(k) != v for k, v in vals.items()):
                cds_rows[slug] = fmt_cds(slug, d)
                for k, v in vals.items():
                    if old.get(k) != v:
                        changed.append(f"  {slug:22s} {k:7s} {old.get(k)} -> {v}")
        g = d.get("c7")
        if g and len(g) >= 12 and cd.C7_FACTORS.get(slug) != g:
            c7_rows[slug] = fmt_c7(slug, g)

    print(f"CDS_VERIFIED rows to write: {len(cds_rows)}")
    print(f"C7_FACTORS   rows to write: {len(c7_rows)}  "
          f"(coverage {len(cd.C7_FACTORS)} -> {len(set(cd.C7_FACTORS) | set(c7_rows))})")
    print(f"individual field changes: {len(changed)}")
    for l in changed[:30]:
        print(l)
    if DRY:
        print("\n--dry: nothing written")
        return
    src, r1, a1 = splice(src, "CDS_VERIFIED", dict(cds_rows))
    src, r2, a2 = splice(src, "C7_FACTORS", dict(c7_rows))
    open(path, "w").write(src)
    print(f"\nwrote candor_data.py: CDS_VERIFIED {r1} replaced / {a1} added; "
          f"C7_FACTORS {r2} replaced / {a2} added")


if __name__ == "__main__":
    main()
