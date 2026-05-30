"""For the 39 inconsistent schools, ask Haiku via web_search:
   1. Which admission rounds does this school actually run? (ED, ED2, EA, REA, RD)
   2. For each round it runs, what's the latest acceptance rate?
   3. For rounds it doesn't run, return null (and we'll remove from ADMISSIONS_DETAIL).

Output: results_round_verify.jsonl

Cost: ~$1.50 (39 schools × ~$0.04 each via Anthropic web_search).
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
RESULTS = HERE / "results_round_verify.jsonl"
LOG = HERE / "run_round_verify.log"

PROMPT = """You are verifying which admission rounds a US college runs and the latest
acceptance rate for each round.

Step 1: Determine which of these admission rounds the college OFFERS:
  - ED1 (Early Decision, first round, binding)
  - ED2 (Early Decision second round, binding)
  - ED (single ED round, binding)
  - EA (Early Action, non-binding, non-restrictive)
  - REA / SCEA (Restrictive Early Action / Single-Choice EA, non-binding but restrictive)
  - RD (Regular Decision)

For some schools, the answer is just "RD" (many public flagships, art schools, etc.).

Sources to consult, in priority order:
  - The school's own admissions page (admissions.<school>.edu)
  - The school's CDS section C21 (Early Decision) and C22 (Early Action)
  - School press release for most recent class

Step 2: For each round the school OFFERS, find the latest published acceptance rate
(Class of 2030 / spring 2026 preferred; else Class of 2029 / CDS 2025-26).

NEVER use US News, Niche, CollegeData, CollegeWiz, Wikipedia, Reddit.

Return JSON ONLY (start with { end with }):

{
  "rounds_offered": ["ED1","ED2","EA","REA","RD"]  // SUBSET — only ones school actually runs
  "rates": {
    "ED1": float (0..1) or null,
    "ED2": float (0..1) or null,
    "ED":  float (0..1) or null,
    "EA":  float (0..1) or null,
    "REA": float (0..1) or null,
    "SCEA": float (0..1) or null,
    "RD":  float (0..1) or null
  },
  "class_year": "2030" | "2029" | "2028" | null,
  "source_url": "primary URL used",
  "confidence": "high" | "medium" | "low",
  "notes": "brief"
}

If the school doesn't run a particular round, OMIT it from rounds_offered AND leave the rate null.
Rates are decimal (0.045 not 4.5%). Do not invent. Use null for unknown rates.
"""


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_done() -> set[str]:
    if not RESULTS.exists():
        return set()
    done = set()
    for line in RESULTS.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("status") == "ok":
                done.add(r["slug"])
        except Exception:
            pass
    return done


def process_one(slug: str, name: str, claude: anthropic.Anthropic) -> dict:
    log(f"-> {slug} ({name})")
    rec = {"slug": slug, "name": name, "ts": time.time()}
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{
                "role": "user",
                "content": (
                    f"College: {name}\n\nWhich admission rounds does this school actually run? "
                    f"For each round it runs, find the latest acceptance rate from an official "
                    f"source (school press release, class profile, or CDS)."
                ),
            }],
        )
        out_text = ""
        for b in resp.content:
            if hasattr(b, "text"):
                out_text = b.text
        rec["raw"] = out_text[:400]
        rec["usage"] = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "web_search_calls": getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) if getattr(resp.usage, "server_tool_use", None) else 0,
        }
        m = re.search(r"\{[\s\S]*\}", out_text)
        if not m:
            rec["status"] = "no_json"
            rec["error"] = out_text[:200]
            return rec
        extracted = json.loads(m.group())
        rec["status"] = "ok"
        rec["extracted"] = extracted
        log(f"   rounds={extracted.get('rounds_offered')} rates={extracted.get('rates')} conf={extracted.get('confidence')}")
    except Exception as ex:
        rec["status"] = "error"
        rec["error"] = str(ex)[:200]
        log(f"   ERR: {ex}")
    return rec


def main():
    if not os.environ.get("ANTHROPIC_KEY"):
        print("ANTHROPIC_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Read flagged list + names from app.py via COLLEGES
    flagged = json.load(open('/tmp/flagged_urls.json'))['flagged']
    import ast
    src = open(HERE.parent.parent / 'app.py').read()
    tree = ast.parse(src)
    by_slug = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'COLLEGES':
                    cs = ast.literal_eval(node.value)
                    by_slug = {c['slug']: c for c in cs}
                    break

    todo = [(s, by_slug[s]['name']) for s in flagged if s in by_slug]
    done = load_done()
    todo = [(s, n) for s, n in todo if s not in done]
    log(f"start: {len(todo)} schools")

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(process_one, s, n, claude): (s, n) for s, n in todo}
        cnt = 0
        for fut in as_completed(futs):
            rec = fut.result()
            with open(RESULTS, "a") as f:
                f.write(json.dumps(rec) + "\n")
            cnt += 1
            if cnt % 5 == 0 or cnt == len(todo):
                log(f"progress: {cnt}/{len(todo)}")
    log(f"done.")


if __name__ == "__main__":
    main()
