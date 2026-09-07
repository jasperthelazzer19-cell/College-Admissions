#!/usr/bin/env python3
"""Cross-check agent-reported CDS data against a deterministic re-read, then
emit a merge proposal.

The agents read CDS PDFs with a model; this re-reads the SAME source_url with
scripts/cds_c7_extract.py (pure coordinate geometry, no model) and compares.
Where they disagree on a C7 factor, the deterministic read wins and the
disagreement is logged. Numeric fields (C1/C9) can't be re-derived this way, so
they're sanity-gated instead: a value outside a plausible range, or a swing of
more than DELTA from what Candor currently holds, gets flagged for eyeballing
rather than silently accepted.

Usage: python3 scripts/cds_verify_merge.py [--delta 0.05]
Reads /tmp/cds/out*.json + /tmp/cds/pilot.json, writes /tmp/cds/proposal.json
and /tmp/cds/report.txt
"""
import glob, json, os, re, sys, traceback, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import candor_data as cd
from scripts.cds_c7_extract import extract as c7_extract

DELTA = float(sys.argv[sys.argv.index("--delta") + 1]) if "--delta" in sys.argv else 0.05
LEVELS = {"very_important", "important", "considered", "not_considered"}
CUR = {c["slug"]: c for c in cd.COLLEGES}
NAMES = {c["slug"]: c["name"] for c in cd.COLLEGES}


DOMAINS = json.load(open("/tmp/cds/domains.json"))


def domain_ok(slug, rec):
    """Domain check — a SIGNAL, not a veto.

    Naively requiring the school's domain rejected a lot of good data, for two
    reasons. First, the auto-derived domain map is itself wrong for schools whose
    slug isn't their domain (alabama -> ua.edu, american -> american.edu,
    arkansas -> uark.edu, and bethune-cookman was mapped to bu.edu, which is
    Boston University). Second, plenty of schools legitimately serve their CDS
    from a CDN (Carleton and Bentley both use S3).

    So the authoritative test is the document's own A1 name (identity_ok); this
    only decides the case where the document doesn't state a name at all.
    """
    dom = DOMAINS.get(slug)
    url = rec.get("source_url") or ""
    if not dom or not url:
        return True, ""
    host = urllib.parse.urlparse(url).netloc.lower()
    root = ".".join(dom.split(".")[-2:])
    if host.endswith(root) or "web.archive.org" in host or "amazonaws.com" in host:
        return True, ""
    # host disagrees with our (possibly wrong) map — let the A1 name decide
    return None, f"served from {host}, expected {dom}"


def _norm(s):
    s = re.sub(r"[^a-z ]", " ", (s or "").lower())
    drop = {"university", "college", "of", "the", "at", "in", "state", "institute",
            "technology", "school", "and", "system", "main", "campus"}
    return {w for w in s.split() if len(w) > 2 and w not in drop}


def identity_ok(slug, rec):
    """Does the PDF's own A1 name match the school we asked for?

    A browser search for 'Pomona common data set' returned CAL POLY POMONA's CDS.
    Every number in it parsed cleanly and passed every range check — a 73.95%
    accept rate landed on Pomona College, which is ~7%. Range gates cannot catch
    a wrong-school error; only comparing the document's own name can.
    """
    got = rec.get("institution")
    if not got:
        return True, ""                      # unknown -> don't block, but flag upstream
    want = _norm(NAMES.get(slug, slug))
    have = _norm(got)
    if not want:
        return True, ""
    overlap = want & have
    if overlap:
        return True, ""
    # accept an acronym match (MIT, UCLA, NYU) before rejecting
    acro = "".join(w[0] for w in sorted(want))
    if len(want) > 1 and acro and acro in re.sub(r"[^a-z]", "", got.lower()):
        return True, ""
    return False, f"claims to be {got!r}, expected {NAMES.get(slug, slug)!r}"


def plausible(rec):
    """Reject values that cannot be real, so a parse slip never lands."""
    bad = []
    a = rec.get("accept")
    if a is not None and not (0.005 <= a <= 1.0):
        bad.append(f"accept={a} out of range")
    for lo, hi, name, rng in (("sat_25", "sat_75", "SAT", (400, 1600)),
                              ("act_25", "act_75", "ACT", (1, 36))):
        l, h = rec.get(lo), rec.get(hi)
        for v, n in ((l, lo), (h, hi)):
            if v is not None and not (rng[0] <= v <= rng[1]):
                bad.append(f"{n}={v} out of range")
        if l is not None and h is not None and l > h:
            bad.append(f"{name} 25th({l}) > 75th({h})")
    ap, ad = rec.get("applicants"), rec.get("admitted")
    if ap and ad and ad > ap:
        bad.append(f"admitted({ad}) > applicants({ap})")
    if ap and ad and a is not None and abs(ad / ap - a) > 0.005:
        bad.append(f"accept {a} != admitted/applicants {ad/ap:.4f}")
    return bad


def main():
    recs, seen = [], set()
    for f in (sorted(glob.glob("/tmp/cds/out*.json"))
              + ["/tmp/cds/pilot.json", "/tmp/cds/browser.json", "/tmp/cds/spider.json",
                 "/tmp/cds/consolidated.json"]):
        if not os.path.exists(f):
            continue
        try:
            for r in json.load(open(f)):
                slug = r.get("slug")
                if not slug:
                    continue
                r["_file"] = os.path.basename(f)
                if slug in seen:
                    # a deterministic re-read supersedes the agent's record, but
                    # keep any C7 the agent had if the re-read couldn't get one
                    if r.get("via") == "consolidated" and r.get("found"):
                        old = next((x for x in recs if x.get("slug") == slug), None)
                        if old is not None:
                            if not r.get("c7") and old.get("c7"):
                                r["c7"] = old["c7"]
                            recs[recs.index(old)] = r
                    continue
                seen.add(slug); recs.append(r)
        except Exception as e:
            print(f"!! {f}: {e}")

    proposal, report = {}, []
    stats = {"found": 0, "notfound": 0, "c7_reread": 0, "c7_fixed": 0,
             "c7_agent_only": 0, "rejected": 0, "flagged": 0}

    for r in recs:
        slug = r["slug"]
        if not r.get("found"):
            stats["notfound"] += 1
            report.append(f"MISS  {slug:22s} {(r.get('notes') or '')[:90]}")
            continue
        stats["found"] += 1
        ok_dom, why_dom = domain_ok(slug, r)
        ok_id, why = identity_ok(slug, r)
        if not ok_id:
            stats["rejected"] += 1
            report.append(f"WRONGSCHOOL {slug:16s} {why}")
            continue
        # Unknown host AND the document never names itself: nothing corroborates
        # that this is the right school, so don't take it.
        if ok_dom is None and not r.get("institution"):
            stats["rejected"] += 1
            report.append(f"UNVERIFIED {slug:17s} {why_dom}, and PDF states no institution name")
            continue
        yr = str(r.get("cds_year") or "")
        if yr and yr[:4].isdigit() and int(yr[:4]) < 2024:
            stats["rejected"] += 1
            report.append(f"STALE {slug:21s} {yr} — older than 2024-25 floor")
            continue
        bad = plausible(r)
        if bad:
            stats["rejected"] += 1
            report.append(f"REJECT {slug:21s} {'; '.join(bad)}")
            continue

        url = r.get("source_url") or ""
        c7 = dict(r.get("c7") or {})
        c7 = {k: v for k, v in c7.items() if v in LEVELS}
        # Deterministic re-read of the SAME document.
        if url.lower().endswith(".pdf"):
            try:
                det, warns = c7_extract(url)
                if det:
                    stats["c7_reread"] += 1
                    diff = [k for k in det if k in c7 and c7[k] != det[k]]
                    if diff:
                        stats["c7_fixed"] += 1
                        report.append(f"C7DIFF {slug:21s} {len(diff)}/18 differ, "
                                      f"deterministic wins: "
                                      + ", ".join(f"{k}:{c7[k]}->{det[k]}" for k in diff[:4]))
                    c7.update(det)           # deterministic read is authoritative
            except Exception as e:
                report.append(f"C7ERR {slug:21s} {type(e).__name__}: {e}")
        elif c7:
            stats["c7_agent_only"] += 1

        cur = CUR.get(slug, {})
        cur_a = (cd.CDS_VERIFIED.get(slug, {}) or {}).get("accept", cur.get("accept"))
        if r.get("accept") is not None and cur_a and abs(r["accept"] - cur_a) > DELTA:
            stats["flagged"] += 1
            report.append(f"SWING {slug:21s} accept {cur_a:.3f} -> {r['accept']:.4f} "
                          f"({(r['accept']-cur_a)*100:+.1f}pp)  {r.get('cds_year')}")

        entry = {k: r[k] for k in ("accept", "sat_25", "sat_75", "act_25", "act_75",
                                   "act_50", "sat_erw_25", "sat_erw_75", "sat_math_25",
                                   "sat_math_75", "pct_submitting_sat", "pct_submitting_act",
                                   "applicants", "admitted", "cds_year", "source_url")
                 if r.get(k) is not None}
        if len(c7) >= 12:
            entry["c7"] = c7
        proposal[slug] = entry

    json.dump(proposal, open("/tmp/cds/proposal.json", "w"), indent=1, sort_keys=True)
    hdr = (f"schools with data: {len(proposal)} | found {stats['found']} | "
           f"not found {stats['notfound']} | rejected {stats['rejected']}\n"
           f"C7 deterministically re-read: {stats['c7_reread']} "
           f"(agent disagreed on {stats['c7_fixed']}) | agent-only C7: {stats['c7_agent_only']}\n"
           f"accept-rate swings > {DELTA*100:.0f}pp: {stats['flagged']}\n" + "=" * 78)
    open("/tmp/cds/report.txt", "w").write(hdr + "\n" + "\n".join(sorted(report)))
    print(hdr)
    print("\n".join(sorted(report)[:60]))


if __name__ == "__main__":
    main()
