"""Cheap top-75 acceptance-rate refresh.

For each school:
  1. If we have a cached press-release URL from prior scrape, fetch it via crwl
     (free), then ask Haiku to extract overall + ED/ED2/EA/REA/RD rates from the
     page markdown. NO web_search — token-only.
  2. If no cached URL, fall back to a single Haiku call with web_search to
     discover and extract in one shot.

Output: results_top75.jsonl + run_top75.log
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
PRIOR = HERE / "results_class2030.jsonl"
TOP75 = HERE / "top75_slugs.json"
RESULTS = HERE / "results_top75.jsonl"
LOG = HERE / "run_top75.log"

EXTRACT_PROMPT = """You are given the text of an official college press release or
admissions page. Extract the acceptance-rate breakdown into structured JSON.

Return JSON ONLY (start with { end with }):

{
  "class_year": "2030" | "2029" | "2028" | null,
  "rounds": ["ED1","ED2","ED","EA","REA","SCEA","RD"] (whichever the school runs; "ED" if just one ED round),
  "rates": {
    "ED1": float (0..1) or null,
    "ED2": float (0..1) or null,
    "ED":  float (0..1) or null,
    "EA":  float (0..1) or null,
    "REA": float (0..1) or null,
    "SCEA": float (0..1) or null,
    "RD":  float (0..1) or null
  },
  "overall_accept": float (0..1) or null,
  "confidence": "high" | "medium" | "low",
  "notes": "brief caveat or empty"
}

Rules:
- Rates are decimal (0.045), not percent.
- If the page reports only OVERALL with no breakdown, leave the round-specific rates null.
- If a round is reported as 'TBD' / 'not yet released' / similar, leave it null.
- Confidence:
    high  — page is the school's own press release / class profile with explicit numbers
    medium — derivable from apps/admits but rate not stated explicitly
    low    — third-party source or numbers seem questionable
- Do NOT invent rates. Use null when not present.
- ALWAYS return the JSON object."""

WEBSEARCH_PROMPT = """You research US college admissions. Find the round-by-round
acceptance rates for the given college's MOST RECENT cycle (Class of 2030 / spring
2026 preferred; else Class of 2029 / CDS 2025-26).

Sources, in priority order:
  1. School's own press release / news post / class profile (preferred)
  2. CDS at the institutional research office
  3. Student newspaper (Crimson, Princetonian, etc.) — acceptable for Ivy Day numbers

NEVER: US News, Niche, CollegeData, CollegeWiz, Wikipedia, Reddit.

Return JSON ONLY (start with { end with }):

{
  "class_year": "2030" | "2029" | "2028" | null,
  "source_url": "URL",
  "rounds": ["ED1","ED2","ED","EA","REA","SCEA","RD"] (whichever apply),
  "rates": {
    "ED1": float or null, "ED2": float or null, "ED": float or null,
    "EA": float or null, "REA": float or null, "SCEA": float or null, "RD": float or null
  },
  "overall_accept": float or null,
  "confidence": "high" | "medium" | "low",
  "notes": "brief caveat"
}

Rules:
- Rates decimal (0.045), not percent.
- Overall accept = total admits / total applications across ALL rounds combined.
- Do NOT invent rates. Set unknown rounds to null.
"""


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_top75() -> list[str]:
    return json.loads(TOP75.read_text())


def load_cached_urls() -> dict[str, str]:
    urls = {}
    if not PRIOR.exists():
        return urls
    for line in PRIOR.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("status") == "ok":
                url = r.get("extracted", {}).get("source_url")
                if url and isinstance(url, str) and url.startswith("http"):
                    urls[r["slug"]] = url
        except Exception:
            pass
    return urls


def load_colleges() -> dict[str, dict]:
    src = APP_PY.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "COLLEGES":
                    return {c["slug"]: c for c in ast.literal_eval(node.value)}
    raise RuntimeError("COLLEGES not found")


def load_done() -> set[str]:
    if not RESULTS.exists():
        return set()
    done = set()
    for line in RESULTS.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("status") in ("ok", "ok_fetched", "ok_websearch"):
                done.add(r["slug"])
        except Exception:
            pass
    return done


def append_result(rec: dict):
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_page_md(url: str, timeout: int = 45) -> str | None:
    """Fetch URL via crwl, return markdown text. Returns None on failure."""
    try:
        # crwl crawl URL -o md  prints markdown to stdout
        proc = subprocess.run(
            ["crwl", "crawl", url, "-o", "md"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout or ""
        # crwl wraps with banners; strip them. Real content typically follows a
        # ``Content`` header or the first non-banner line.
        return out if len(out) > 200 else None
    except Exception:
        return None


def haiku_extract_from_text(text: str, name: str, claude: anthropic.Anthropic) -> tuple[dict | None, dict]:
    """Send page text + extract prompt to Haiku, no web_search. Returns (parsed_json, usage)."""
    # Truncate aggressively — admissions pages typically have the rate in the first ~5k chars.
    truncated = text[:12000]
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        system=[{"type": "text", "text": EXTRACT_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"College: {name}\n\nPage content below — extract the rates:\n\n```\n{truncated}\n```",
        }],
    )
    out_text = ""
    for b in resp.content:
        if hasattr(b, "text"):
            out_text = b.text
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    m = re.search(r"\{[\s\S]*\}", out_text)
    if not m:
        return None, usage
    try:
        return json.loads(m.group()), usage
    except Exception:
        return None, usage


def haiku_websearch(name: str, claude: anthropic.Anthropic) -> tuple[dict | None, dict]:
    """Fallback: one Haiku call with web_search."""
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        system=[{"type": "text", "text": WEBSEARCH_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{
            "role": "user",
            "content": f"College: {name}\n\nFind the round-by-round acceptance rates for this college's most recent cycle.",
        }],
    )
    out_text = ""
    for b in resp.content:
        if hasattr(b, "text"):
            out_text = b.text
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "web_search_calls": getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0),
    }
    m = re.search(r"\{[\s\S]*\}", out_text)
    if not m:
        return None, usage
    try:
        return json.loads(m.group()), usage
    except Exception:
        return None, usage


def process_one(slug: str, name: str, cached_url: str | None, claude: anthropic.Anthropic) -> dict:
    rec = {"slug": slug, "name": name, "ts": time.time()}

    if cached_url:
        log(f"-> {slug}: fetch {cached_url}")
        text = fetch_page_md(cached_url)
        if text and len(text) > 500:
            extracted, usage = haiku_extract_from_text(text, name, claude)
            if extracted and (
                extracted.get("overall_accept") is not None
                or any(v is not None for v in (extracted.get("rates") or {}).values())
            ):
                rec["status"] = "ok_fetched"
                rec["source"] = "crwl+haiku"
                rec["source_url"] = cached_url
                rec["extracted"] = extracted
                rec["usage"] = usage
                r = extracted.get("rates") or {}
                log(f"  ok (fetch): class={extracted.get('class_year')} "
                    f"overall={extracted.get('overall_accept')} ED1={r.get('ED1')} ED2={r.get('ED2')} "
                    f"ED={r.get('ED')} EA={r.get('EA')} REA={r.get('REA')} RD={r.get('RD')} "
                    f"conf={extracted.get('confidence')}")
                return rec
            else:
                log(f"  fetch+extract returned nothing useful -> falling back to web_search")
        else:
            log(f"  crwl fetch failed or too short -> web_search fallback")

    # Fallback: web_search
    log(f"-> {slug}: web_search fallback")
    extracted, usage = haiku_websearch(name, claude)
    if extracted:
        rec["status"] = "ok_websearch"
        rec["source"] = "haiku+websearch"
        rec["source_url"] = extracted.get("source_url")
        rec["extracted"] = extracted
        rec["usage"] = usage
        r = extracted.get("rates") or {}
        log(f"  ok (search): class={extracted.get('class_year')} "
            f"overall={extracted.get('overall_accept')} ED1={r.get('ED1')} ED2={r.get('ED2')} "
            f"ED={r.get('ED')} EA={r.get('EA')} REA={r.get('REA')} RD={r.get('RD')} "
            f"conf={extracted.get('confidence')}")
    else:
        rec["status"] = "no_json"
        rec["usage"] = usage
        log(f"  ERR: no json from websearch fallback")
    return rec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_KEY"):
        print("ANTHROPIC_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("crwl"):
        print("crwl not on PATH", file=sys.stderr)
        sys.exit(1)

    top75 = load_top75()
    colleges = load_colleges()
    cached_urls = load_cached_urls()

    if args.resume:
        done = load_done()
        before = len(top75)
        top75 = [s for s in top75 if s not in done]
        log(f"resume: {before - len(top75)} done, {len(top75)} to go")

    if args.limit:
        top75 = top75[: args.limit]

    log(f"start: {len(top75)} schools, concurrency={args.concurrency}, "
        f"cached_urls available: {sum(1 for s in top75 if s in cached_urls)}")

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    work = []
    for slug in top75:
        if slug not in colleges:
            log(f"skip {slug}: not in COLLEGES")
            continue
        work.append((slug, colleges[slug]["name"], cached_urls.get(slug)))

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(process_one, slug, name, url, claude): slug
                for slug, name, url in work}
        done_count = 0
        for fut in as_completed(futs):
            rec = fut.result()
            append_result(rec)
            done_count += 1
            if done_count % 5 == 0 or done_count == len(work):
                log(f"progress: {done_count}/{len(work)}")
    log(f"done. wrote {RESULTS.name}")


if __name__ == "__main__":
    main()
