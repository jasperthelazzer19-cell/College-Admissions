"""Class-of-2030 (spring 2026) acceptance-rate refresh for top tier-1/tier-2 schools.

Differs from scrape_cds.py by prompting Claude to PREFER the very newest published
admission rate — Class of 2030 press releases / class profiles published spring 2026
— and only fall back to CDS 2025-26 / 2024-25 if no Class of 2030 number is public yet.

Outputs to a SEPARATE results file so it doesn't clobber the May 2026 CDS run.

Run:
  ANTHROPIC_KEY=sk-ant-... uv run --with anthropic python scrape_class2030.py
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
RESULTS = HERE / "results_class2030.jsonl"
LOG = HERE / "run_class2030.log"

EXTRACTION_PROMPT = """You research US college admissions data from official sources.

Goal: find the MOST RECENT overall acceptance rate for the given college, with strong
preference for the freshest figure available.

Priority order (use the FIRST one you find — do not regress to older years if a newer
number is public):

1. Class of 2030 acceptance rate (admitted spring 2026, entering fall 2026). These are
   usually in a press release, "admissions decisions" news post, or class profile page
   published March-May 2026. Examples of where they live:
     - <school>.edu/news/<spring-2026-press-release>
     - admissions.<school>.edu/class-profile
2. CDS 2025-26 (Class of 2029, admitted spring 2025) — Common Data Set, institutional
   research office.
3. CDS 2024-25 (Class of 2028) if nothing newer exists.

Never use US News, Niche, CollegeData, Wikipedia, or Reddit. Stick to .edu / institutional
research / admissions pages. The Crimson, Daily Princetonian, and similar student
newspapers ARE acceptable for Ivy Day numbers when the school hasn't published a press
release yet, but flag confidence accordingly.

Return ONLY a JSON object — no prose, no markdown fences:

{
  "class_year": "2030" | "2029" | "2028" | null,    // which incoming class this rate is for
  "source_url": "URL of the page you used",
  "source_type": "press_release" | "class_profile" | "cds" | "student_newspaper" | "other",
  "applications": int or null,
  "admits": int or null,
  "accept": float or null,            // overall acceptance as decimal 0..1
  "ed_accept": float or null,         // Early Decision rate if reported
  "ea_accept": float or null,         // Early Action rate if reported
  "rd_accept": float or null,         // Regular Decision rate if reported
  "confidence": "high" | "medium" | "low",
  "notes": "short caveat string or empty"
}

Rules:
- accept must be decimal (0.046), not percent.
- Do NOT invent numbers. Use null when unsure. Lower confidence accordingly.
- If the Class of 2030 figure is a press-release-only number that doesn't yet have full
  applications/admits breakdown, that's fine — return the rate with applications/admits=null.
- ALWAYS return the JSON object even if you found nothing.
- Return JSON object ONLY — start with { and end with }."""


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
    raise RuntimeError("COLLEGES not found in app.py")


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
    for field in ["accept", "ed_accept", "ea_accept", "rd_accept"]:
        cur = current.get(field)
        new = scraped.get(field)
        if new is None:
            out[field] = {"current": cur, "new": None, "delta": None}
        elif cur is None:
            out[field] = {"current": None, "new": new, "delta": "+"}
        elif abs((cur or 0) - (new or 0)) < 1e-6:
            out[field] = {"current": cur, "new": new, "delta": "="}
        else:
            out[field] = {"current": cur, "new": new, "delta": f"{(new - cur):+.4f}"}
    return out


def process_one(c: dict, claude: anthropic.Anthropic) -> dict:
    slug, name = c["slug"], c["name"]
    log(f"-> {slug} ({name})")
    rec = {"slug": slug, "name": name, "ts": time.time()}
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
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
                "content": (
                    f"College: {name}\n\n"
                    f"Find this school's Class of 2030 (spring 2026) acceptance rate from "
                    f"an official press release or class profile. If not yet published, "
                    f"return the freshest CDS figure you can find."
                ),
            }],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text = block.text
        rec["raw"] = text[:500]
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
        log(f"  ok: class={e.get('class_year')} accept={e.get('accept')} src_type={e.get('source_type')} conf={e.get('confidence')}")
    except Exception as ex:
        rec["status"] = "error"
        rec["error"] = str(ex)[:300]
        log(f"  ERROR: {ex}")
    return rec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--slugs", type=str, default=None,
                        help="comma-separated slugs; default = all tier-1+tier-2 schools")
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
    if args.limit:
        colleges = colleges[: args.limit]

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
