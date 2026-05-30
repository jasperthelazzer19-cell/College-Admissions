"""Pre-generate school summaries for all 334 schools, mirroring the live
get_school_summary() prompt so output matches the existing voice.

Output: school_summaries.json — {slug: body, ...}
Cost: ~$1.70 in Haiku tokens.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
APP_PY = HERE.parent.parent / "app.py"
OUT = HERE / "school_summaries.json"
LOG = HERE / "summaries.log"

REGION_MAP = {
    "Massachusetts": "Northeast", "Connecticut": "Northeast", "Rhode Island": "Northeast",
    "Vermont": "Northeast", "New Hampshire": "Northeast", "Maine": "Northeast",
    "New York": "Northeast", "New Jersey": "Northeast", "Pennsylvania": "Northeast",
    "District of Columbia": "Mid-Atlantic", "Maryland": "Mid-Atlantic", "Virginia": "Mid-Atlantic",
    "Delaware": "Mid-Atlantic", "West Virginia": "Mid-Atlantic",
    "North Carolina": "South", "South Carolina": "South", "Georgia": "South", "Florida": "South",
    "Alabama": "South", "Tennessee": "South", "Kentucky": "South", "Mississippi": "South",
    "Louisiana": "South", "Arkansas": "South", "Texas": "South", "Oklahoma": "South",
    "Ohio": "Midwest", "Indiana": "Midwest", "Illinois": "Midwest", "Michigan": "Midwest",
    "Wisconsin": "Midwest", "Minnesota": "Midwest", "Iowa": "Midwest", "Missouri": "Midwest",
    "Kansas": "Midwest", "Nebraska": "Midwest", "North Dakota": "Midwest", "South Dakota": "Midwest",
    "California": "West Coast", "Oregon": "West Coast", "Washington": "West Coast",
    "Nevada": "West", "Arizona": "West", "New Mexico": "West", "Colorado": "West", "Utah": "West",
    "Idaho": "West", "Montana": "West", "Wyoming": "West", "Alaska": "West", "Hawaii": "West",
    "Puerto Rico": "Caribbean",
}


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_colleges() -> list[dict]:
    text = APP_PY.read_text()
    s = text.find("COLLEGES = [")
    e = text.find("\nCOLLEGES_BY_SLUG")
    return eval(text[s:e].split("=", 1)[1].strip().rstrip(","), {"__builtins__": {}}, {})


def make_user_msg(c: dict) -> str:
    region = REGION_MAP.get(c.get("state", ""), "US")
    return (
        f"School: {c['name']} ({c.get('state','?')}, {region})\n"
        f"Type: {c['type']}, ~{c.get('size','?'):,} undergrads, ${c.get('tuition',0):,} sticker\n"
        f"Acceptance rate: {round(c['accept']*100,1)}%\n"
        f"Popular majors: {', '.join(c.get('majors',[]))}\n"
        f"Existing 1-line description: {c.get('desc','')}\n\n"
        f"Write 2-3 paragraphs about {c['name']}. Cover: what the school is actually known for, "
        f"who thrives there, what the social scene looks like, what the location adds or doesn't, "
        f"real academic strengths.\n\n"
        f"Hard rules:\n"
        f"- Write like a knowledgeable older friend, not marketing copy\n"
        f"- No words like 'renowned', 'esteemed', 'boasts', 'vibrant', 'rigorous' (real talk only)\n"
        f"- No em-dashes (use commas, periods, parens)\n"
        f"- Be specific: name programs, traditions, recruiting pipelines, real downsides\n"
        f"- 200-280 words. No headers, no bullets, no superlatives."
    )


SYSTEM = (
    "You write substantive, honest college overviews like a knowledgeable older sibling "
    "explaining a school to a high schooler. Specific, slightly opinionated, acknowledges "
    "downsides. No marketing language. No em-dashes."
)


def gen_one(c: dict, claude: anthropic.Anthropic) -> tuple[str, str]:
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": make_user_msg(c)}],
        )
        body = resp.content[0].text.strip()
        return c["slug"], body
    except Exception as e:
        log(f"  ERROR {c['slug']}: {e}")
        return c["slug"], ""


def main():
    if not os.environ.get("ANTHROPIC_KEY"):
        sys.exit("ANTHROPIC_KEY not set")
    colleges = load_colleges()
    log(f"start: {len(colleges)} schools")

    # Resume support: load any existing results
    existing = {}
    if OUT.exists():
        existing = json.loads(OUT.read_text())
        colleges = [c for c in colleges if c["slug"] not in existing or not existing.get(c["slug"])]
        log(f"resume: {len(existing)} done, {len(colleges)} to go")

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

    results = dict(existing)
    in_flight = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(gen_one, c, claude): c for c in colleges}
        done = 0
        for fut in as_completed(futs):
            slug, body = fut.result()
            results[slug] = body
            done += 1
            if body:
                log(f"  ok {slug} ({len(body)} chars)")
            # Save snapshot every 10 done
            if done % 10 == 0:
                OUT.write_text(json.dumps(results, indent=2))
                log(f"progress: {done}/{len(colleges)}")

    OUT.write_text(json.dumps(results, indent=2))
    ok = sum(1 for v in results.values() if v)
    log(f"done. {ok}/{len(results)} succeeded. wrote {OUT.name}")


if __name__ == "__main__":
    main()
