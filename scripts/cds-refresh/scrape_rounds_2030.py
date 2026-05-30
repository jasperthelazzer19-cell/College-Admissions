"""Class-of-2030 round-by-round acceptance rate scrape.

Pulls ED1, ED2, EA, REA, RD rates for the top tier-1+tier-2 schools, preferring
official Class of 2030 (spring 2026) press releases when available.

Output: results_rounds_2030.jsonl + run_rounds_2030.log (separate from prior runs).
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
RESULTS = HERE / "results_rounds_2030.jsonl"
LOG = HERE / "run_rounds_2030.log"

PROMPT = """You research US college admissions data. I need ROUND-BY-ROUND acceptance rates
for the given school's MOST RECENT admission cycle (Class of 2030 / spring 2026 decisions
if available, else Class of 2029 / spring 2025).

Many selective US colleges offer multiple admission rounds. Common patterns:
  - "ED1 + ED2 + RD"          (e.g. NYU, Vanderbilt, Emory)
  - "ED + RD"                  (e.g. Duke, Cornell)
  - "EA + RD" non-restrictive  (e.g. MIT, Georgetown, Notre Dame)
  - "REA + RD" restrictive     (e.g. Harvard, Yale, Stanford, Princeton)
  - "EA + ED + RD"             (e.g. some LACs)
  - "RD only"                  (e.g. some publics)

Find OFFICIAL acceptance rate for EACH round the school runs. Sources, priority:
  1. School's own press release / admissions news post (Class of 2030 preferred)
  2. School's class profile / admissions statistics page
  3. Student newspaper covering Ivy Day (Crimson, Princetonian, Cornell Sun, etc.) — acceptable
     when school hasn't posted yet
  4. Common Data Set (most-recent year only)

NEVER: US News, Niche, CollegeData, CollegeWiz, Wikipedia, Reddit, Quora.

Return JSON ONLY:

{
  "class_year": "2030" | "2029" | null,
  "source_url": "primary URL used",
  "rounds": ["ED1","ED2","EA","REA","RD"] (whichever apply; null if you can't determine),
  "rates": {
    "ED1": float (0..1) or null,
    "ED2": float (0..1) or null,
    "EA":  float (0..1) or null,
    "REA": float (0..1) or null,
    "RD":  float (0..1) or null
  },
  "overall_accept": float (0..1) or null,
  "confidence": "high" | "medium" | "low",
  "notes": "brief caveats"
}

If the school only runs RD, return rounds=["RD"] and just the RD rate.
If a round wasn't reported for the latest cycle, set that rate to null.
Do not invent rates. Set confidence accordingly.
Return JSON object ONLY — start with { and end with }."""


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_colleges() -> list[dict]:
    src = APP_PY.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "COLLEGES":
                    return ast.literal_eval(node.value)
    raise RuntimeError("COLLEGES not found")


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


def append_result(rec: dict):
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec) + "\n")


def process_one(c: dict, claude: anthropic.Anthropic) -> dict:
    slug, name = c["slug"], c["name"]
    log(f"-> {slug} ({name})")
    rec = {"slug": slug, "name": name, "ts": time.time()}
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{
                "role": "user",
                "content": (
                    f"College: {name}\n\n"
                    f"Find the official Class of 2030 (or most recent) round-by-round "
                    f"acceptance rates: ED1, ED2, EA, REA, RD as applicable. Return the "
                    f"breakdown for whichever rounds this school actually runs."
                ),
            }],
        )
        text = ""
        for b in resp.content:
            if hasattr(b, "text"):
                text = b.text
        rec["raw"] = text[:500]
        rec["usage"] = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            rec["status"] = "no_json"
            rec["error"] = text[:300]
            return rec
        extracted = json.loads(m.group())
        rec["status"] = "ok"
        rec["extracted"] = extracted
        rates = extracted.get("rates") or {}
        log(f"  ok: class={extracted.get('class_year')} rounds={extracted.get('rounds')} "
            f"ED1={rates.get('ED1')} ED2={rates.get('ED2')} EA={rates.get('EA')} "
            f"REA={rates.get('REA')} RD={rates.get('RD')} conf={extracted.get('confidence')}")
    except Exception as ex:
        rec["status"] = "error"
        rec["error"] = str(ex)[:300]
        log(f"  ERROR: {ex}")
    return rec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slugs", type=str, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_KEY"):
        print("ANTHROPIC_KEY not set", file=sys.stderr)
        sys.exit(1)

    colleges = load_colleges()
    if args.slugs:
        wanted = set(args.slugs.split(","))
        colleges = [c for c in colleges if c["slug"] in wanted]
    else:
        colleges = [c for c in colleges if c.get("tier") in (1, 2)]

    if args.resume:
        done = load_done()
        before = len(colleges)
        colleges = [c for c in colleges if c["slug"] not in done]
        log(f"resume: {before - len(colleges)} done, {len(colleges)} to go")

    log(f"start: {len(colleges)} colleges, concurrency={args.concurrency}")
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(process_one, c, claude): c for c in colleges}
        done_count = 0
        for fut in as_completed(futs):
            rec = fut.result()
            append_result(rec)
            done_count += 1
            if done_count % 5 == 0 or done_count == len(colleges):
                log(f"progress: {done_count}/{len(colleges)}")
    log(f"done. wrote {RESULTS.name}")


if __name__ == "__main__":
    main()
