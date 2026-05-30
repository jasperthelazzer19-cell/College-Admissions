"""Refresh Common Data Set figures for every college in COLLEGES via Claude web search.

Strategy per college: one Claude call with the web_search tool — Claude searches
for the school's most recent CDS, reads it, and returns structured JSON.

Run:
  ANTHROPIC_KEY=sk-ant-... uv run --with anthropic python scrape_cds.py
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
RESULTS = HERE / "results.jsonl"
LOG = HERE / "run.log"

EXTRACTION_PROMPT = """You research US college admissions data from official sources.

For the given college, find its MOST RECENT Common Data Set (CDS) — usually at the school's
institutional research office, e.g. /oir/cds, /ir/common-data-set, etc.

If the CDS is only available as a PDF you cannot fully read, DO NOT give up — search for the
school's separately published admissions statistics, class profile, or "by the numbers" page
(usually on the admissions site). These pages typically have the same data in HTML. Examples:
  - college.harvard.edu/admissions/admissions-statistics
  - admission.princeton.edu/how-apply/admission-statistics

Never use US News, Niche, CollegeData, Wikipedia, or Reddit as a source.

Search the web, read the page, and return ONLY a JSON object with these fields:

{
  "cds_year": "YYYY-YY of CDS reported (e.g. '2024-25') or null",
  "source_url": "URL of the page you used",
  "applications": int or null,
  "admits": int or null,
  "accept": float or null,            # overall acceptance as decimal 0..1
  "ed_accept": float or null,         # Early Decision rate if reported
  "ea_accept": float or null,         # Early Action rate if reported
  "rd_accept": float or null,         # Regular Decision rate if reported
  "sat_25": int or null,              # SAT total 25th pct (EBRW + Math)
  "sat_75": int or null,              # SAT total 75th pct
  "act_25": int or null,              # ACT composite 25th pct
  "act_75": int or null,              # ACT composite 75th pct
  "size": int or null,                # undergraduate enrollment
  "tuition": int or null,             # tuition + required fees ONLY (NOT room/board, NOT total cost of attendance). CDS section G0/G1.
  "is_test_optional": true or false or null,
  "confidence": "high" | "medium" | "low",
  "notes": "short caveat string or empty"
}

Rules:
- Prefer the MOST RECENT year you can find. Report which year in cds_year.
- accept must be decimal (0.046), not percent.
- If only SAT EBRW + Math are split, sum the 25ths and 75ths.
- If the school is test-optional and SAT/ACT is for submitters only, still report it but note that.
- Do NOT invent numbers. Use null when unsure. Lower confidence accordingly.
- ALWAYS return the JSON object — even if you found nothing, return it with all fields null,
  confidence="low", and notes explaining what you searched for and why it failed.
- Return JSON object ONLY — no prose before or after, no markdown fences. Start with { and end with }."""


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_colleges() -> list[dict]:
    text = APP_PY.read_text()
    start = text.find("COLLEGES = [")
    end = text.find("\nCOLLEGES_BY_SLUG")
    if start < 0 or end < 0:
        raise RuntimeError("Could not locate COLLEGES list in app.py")
    block = text[start:end].split("=", 1)[1].strip().rstrip(",")
    return eval(block, {"__builtins__": {}}, {})


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


def diff_summary(current: dict, scraped: dict) -> dict:
    out = {}
    for field in ["accept", "sat_25", "sat_75", "act_25", "act_75", "size", "tuition"]:
        cur = current.get(field)
        new = scraped.get(field)
        if new is None:
            out[field] = {"current": cur, "new": None, "delta": None}
        elif cur is None:
            out[field] = {"current": None, "new": new, "delta": "+"}
        elif abs((cur or 0) - (new or 0)) < 1e-6:
            out[field] = {"current": cur, "new": new, "delta": "="}
        else:
            out[field] = {"current": cur, "new": new, "delta": f"{new - cur:+g}"}
    return out


def process_one(c: dict, claude: anthropic.Anthropic) -> dict:
    slug, name = c["slug"], c["name"]
    log(f"→ {slug} ({name})")
    rec = {"slug": slug, "name": name, "ts": time.time()}
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": EXTRACTION_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            messages=[{
                "role": "user",
                "content": f"College: {name}\n\nFind and return the latest Common Data Set figures for this college.",
            }],
        )
        # Grab the last text block (after tool use)
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text = block.text
        rec["raw"] = text[:500]  # keep a tail for debugging
        rec["usage"] = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "server_tool_uses": getattr(resp.usage, "server_tool_use", None).__dict__ if getattr(resp.usage, "server_tool_use", None) else None,
        }
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            rec["status"] = "no_json"
            rec["error"] = text[:300]
            return rec
        extracted = json.loads(m.group())
        rec["status"] = "ok"
        rec["extracted"] = extracted
        rec["diff"] = diff_summary(c, extracted)
        e = extracted
        log(f"  ok: yr={e.get('cds_year')} accept={e.get('accept')} sat={e.get('sat_25')}-{e.get('sat_75')} src={(e.get('source_url') or '')[:80]}")
    except Exception as ex:
        rec["status"] = "error"
        rec["error"] = str(ex)[:300]
        log(f"  ERROR: {ex}")
    return rec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
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
    elif args.limit:
        colleges = colleges[: args.limit]

    if args.resume:
        done = load_done()
        before = len(colleges)
        colleges = [c for c in colleges if c["slug"] not in done]
        log(f"resume: {before - len(colleges)} done, {len(colleges)} to go")

    log(f"start: {len(colleges)} colleges, concurrency={args.concurrency}")
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

    # Process with thread-pool for concurrency (anthropic SDK is sync)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(process_one, c, claude): c for c in colleges}
        done_count = 0
        for fut in as_completed(futs):
            rec = fut.result()
            append_result(rec)
            done_count += 1
            if done_count % 10 == 0 or done_count == len(colleges):
                log(f"progress: {done_count}/{len(colleges)}")
    log(f"done. wrote {RESULTS.name}")


if __name__ == "__main__":
    main()
