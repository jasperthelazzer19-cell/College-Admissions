"""Cheap CDS + C7-factor refresh for the top 100 (the schools not already pulled by
the high-accuracy agent run). One Haiku-4.5 + web_search call per school: find the
most recent Common Data Set (2025-26 preferred, else 2024-25), extract overall accept
rate, SAT/ACT 25/75, the cycle year, and the full C7 factor-importance grid.

Runs on the metered ANTHROPIC_KEY (Haiku is cheap). Output: results_cds_c7.jsonl.
Honest caveat: Haiku reading a CDS is less reliable on the C7 grid than a full-model
PDF parse — confidence flags + the patch guardrails + the human diff are the safety net.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, ast
from pathlib import Path
import anthropic

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
TOP100 = Path("/tmp/top100.json")
AGENT_RESULTS = HERE / "results_top100_2026.jsonl"   # the 31 already done by agents
RESULTS = HERE / "results_cds_c7.jsonl"
LOG = HERE / "run_cds_c7.log"
MODEL = os.environ.get("CDS_MODEL", "claude-sonnet-4-6")

C7_KEYS = ["rigor_of_record","class_rank","gpa","test_scores","essay","recommendations",
           "interview","extracurriculars","talent_ability","character","first_generation",
           "legacy","geographical_residence","state_residency","religious_affiliation",
           "volunteer_work","work_experience","level_of_interest"]

PROMPT = """You pull official Common Data Set (CDS) facts for one US college. Accuracy is
critical — this feeds a real product.

METHOD (do this, don't skim snippets):
1. Search for the school's OFFICIAL CDS at its institutional-research office. Prefer the
   2025-2026 cycle (Fall 2025 / Class of 2029); fall back to 2024-25 only if 2025-26 isn't
   published yet.
2. Actually OPEN and READ the CDS document (PDF, xlsx, or Tableau). If the official link
   is dead/blocked (Cloudflare, 404), try a Wayback Machine snapshot of it or the IR page.
3. Read these sections directly: C1 (applicants/admits -> compute accept rate), C9 (SAT/ACT
   25th & 75th composite percentiles), and C7 (the factor-importance grid — read which
   column each factor's X is in: Very Important / Important / Considered / Not Considered).

Extract and return JSON ONLY (start { end }):
{
 "cds_year": "2025-26" | "2024-25" | null,
 "accept": <overall admit rate decimal 0..1> | null,
 "sat_25": <int 400-1600>|null, "sat_75": <int>|null,
 "act_25": <int 1-36>|null, "act_75": <int>|null,
 "c7_factors": {"rigor_of_record":?, "class_rank":?, "gpa":?, "test_scores":?, "essay":?,
   "recommendations":?, "interview":?, "extracurriculars":?, "talent_ability":?, "character":?,
   "first_generation":?, "legacy":?, "geographical_residence":?, "state_residency":?,
   "religious_affiliation":?, "volunteer_work":?, "work_experience":?, "level_of_interest":?},
 "source_url": "<the CDS URL you used>",
 "confidence": "high"|"med"|"low",
 "notes": "which cycle, anything odd"
}
Each c7 value EXACTLY one of: "very_important","important","considered","not_considered", or null.
Rules: rates are decimals not percent. NEVER invent values — use null if you can't verify from the
official CDS. confidence "high" ONLY if read from the official CDS doc. Test-optional schools usually
mark test_scores "considered" or "not_considered". Return ONLY the JSON object."""


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_colleges():
    src = APP_PY.read_text()
    # COLLEGES is imported from candor_data; load that module's file directly
    cd = (HERE.parent.parent / "candor_data.py").read_text()
    tree = ast.parse(cd)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "COLLEGES":
                    return {c["slug"]: c for c in ast.literal_eval(node.value)}
    raise RuntimeError("COLLEGES not found")


def already_done() -> set[str]:
    done = set()
    for f in (AGENT_RESULTS, RESULTS):
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                    done.add(r.get("slug"))
                except Exception:
                    pass
    return done


def extract_one(slug, name, claude) -> dict:
    rec = {"slug": slug, "name": name, "ts": time.time()}
    try:
        resp = claude.messages.create(
            model=MODEL, max_tokens=1600,
            system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": f"College: {name} (slug: {slug}). Find its most recent official Common Data Set and extract the JSON."}],
        )
        out = "".join(b.text for b in resp.content if hasattr(b, "text"))
        rec["usage"] = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
                        "ws": getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0)}
        m = re.search(r"\{[\s\S]*\}", out)
        if not m:
            rec["status"] = "no_json"; return rec
        d = json.loads(m.group())
        d["slug"] = slug
        rec["extracted"] = d
        rec["status"] = "ok"
        log(f"  ok {slug}: {d.get('cds_year')} accept={d.get('accept')} conf={d.get('confidence')}")
    except Exception as e:
        rec["status"] = "err"; rec["error"] = str(e)[:200]
        log(f"  ERR {slug}: {str(e)[:120]}")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--only", type=str, default=None, help="comma-separated slugs to test")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_KEY not set", file=sys.stderr); sys.exit(1)

    top = json.loads(TOP100.read_text())
    colleges = load_colleges()
    done = already_done()
    if args.only:
        want = args.only.split(",")
        todo = [{"slug": s} for s in want if s in colleges]
    else:
        todo = [c for c in top if c["slug"] not in done and c["slug"] in colleges]
    if args.limit:
        todo = todo[:args.limit]
    log(f"start: {len(todo)} schools to pull (skipping {len(done)} already done), concurrency={args.concurrency}")

    claude = anthropic.Anthropic(api_key=key)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(extract_one, c["slug"], colleges[c["slug"]]["name"], claude): c["slug"] for c in todo}
        n = 0
        for fut in as_completed(futs):
            rec = fut.result()
            with open(RESULTS, "a") as f:
                f.write(json.dumps(rec) + "\n")
            n += 1
            if n % 5 == 0 or n == len(todo):
                log(f"progress: {n}/{len(todo)}")
    # cost tally
    tot_in = tot_out = tot_ws = 0
    for line in RESULTS.read_text().splitlines():
        try:
            u = json.loads(line).get("usage") or {}
            tot_in += u.get("in", 0); tot_out += u.get("out", 0); tot_ws += u.get("ws", 0)
        except Exception:
            pass
    # Haiku 4.5 ~ $1/M in, $5/M out; web_search ~ $10/1k
    est = tot_in/1e6*1.0 + tot_out/1e6*5.0 + tot_ws/1000*10
    log(f"DONE. tokens in={tot_in} out={tot_out} websearches={tot_ws}  est_cost=${est:.2f}")


if __name__ == "__main__":
    main()
