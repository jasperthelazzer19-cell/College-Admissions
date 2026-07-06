"""Cheap CDS + C7 pull: discover the CDS PDF URL with one tiny Haiku web_search, then
DOWNLOAD and PARSE the PDF locally (free, pdfplumber) and send only the relevant
sections (C1 admits, C7 factor grid, C9 test scores) to Sonnet for structured extraction.

This avoids the expensive pattern of letting web_search re-read whole PDFs as input tokens.
Runs on ANTHROPIC_KEY. Output: results_cds_cheap.jsonl. Hard-capped to a fixed slug list.
"""
from __future__ import annotations
import os, sys, json, re, time, tempfile
from pathlib import Path
import requests, pdfplumber, anthropic

HERE = Path(__file__).parent
RESULTS = HERE / "results_cds_cheap.jsonl"
LOG = HERE / "run_cds_cheap.log"
HAIKU = "claude-haiku-4-5-20251001"
SONNET = os.environ.get("CDS_MODEL", "claude-sonnet-4-6")

# The exact 32 Jasper asked for (hard cap — cost can't exceed this many schools).
TARGETS = {
 "purdue":"Purdue","wisc":"University of Wisconsin","umd":"University of Maryland",
 "tamu":"Texas A&M","uw":"University of Washington","uiuc":"University of Illinois Urbana-Champaign",
 "ucdavis":"UC Davis","rutgers":"Rutgers University New Brunswick","sdsu":"San Diego State University",
 "penn-state":"Penn State University Park","msu":"Michigan State University","sc":"University of South Carolina",
 "auburn":"Auburn University","fsu":"Florida State University","rpi":"Rensselaer Polytechnic Institute",
 "ucsc":"UC Santa Cruz","utk":"University of Tennessee Knoxville","clemson":"Clemson University",
 "iu":"Indiana University Bloomington","smu":"Southern Methodist University","pitt":"University of Pittsburgh",
 "rochester":"University of Rochester","elon":"Elon University","osu":"Ohio State University",
 "pepperdine":"Pepperdine University","syracuse":"Syracuse University","cu-boulder":"University of Colorado Boulder",
 "baylor":"Baylor University","fordham":"Fordham University","gwu":"George Washington University",
 "miami-ohio":"Miami University Ohio","umass":"University of Massachusetts Amherst",
}

EXTRACT = """You are given the text of a college's official Common Data Set (CDS). Extract
EXACTLY this JSON (start { end }), using ONLY what's in the text — null for anything absent,
never guess:
{
 "cds_year":"2025-26"|"2024-25"|"2023-24"|null,
 "accept":<overall admit rate decimal, = total admits / total applicants from section C1>|null,
 "sat_25":<int>|null,"sat_75":<int>|null,"act_25":<int>|null,"act_75":<int>|null,
 "c7_factors":{"rigor_of_record":?,"class_rank":?,"gpa":?,"test_scores":?,"essay":?,"recommendations":?,
   "interview":?,"extracurriculars":?,"talent_ability":?,"character":?,"first_generation":?,"legacy":?,
   "geographical_residence":?,"state_residency":?,"religious_affiliation":?,"volunteer_work":?,
   "work_experience":?,"level_of_interest":?},
 "confidence":"high"|"med"|"low","notes":"cycle + caveats"
}
c7 values EXACTLY one of: "very_important","important","considered","not_considered", or null.
In section C7 each factor row has an X in one column (Very Important/Important/Considered/Not Considered) —
read the column. Return ONLY the JSON."""


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"; print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")


def find_url(name, claude) -> tuple[str | None, dict]:
    r = claude.messages.create(
        model=HAIKU, max_tokens=300,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content": f"Find the direct URL of {name}'s most recent official Common Data Set PDF (prefer 2025-2026, else 2024-2025), from its institutional research office. Reply with ONLY the .pdf URL."}],
    )
    out = "".join(b.text for b in r.content if hasattr(b, "text"))
    usage = {"in": r.usage.input_tokens, "out": r.usage.output_tokens,
             "ws": getattr(getattr(r.usage, "server_tool_use", None), "web_search_requests", 0)}
    m = re.search(r"https?://[^\s)\]]+\.pdf", out, re.I) or re.search(r"https?://[^\s)\]]+", out)
    return (m.group(0) if m else None), usage


def get_pdf_text(url) -> str | None:
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}, timeout=40)
        if resp.status_code != 200 or b"%PDF" not in resp.content[:1024]:
            return None
        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tf.write(resp.content); tf.close()
        chunks = []
        with pdfplumber.open(tf.name) as pdf:
            for pg in pdf.pages:
                t = pg.extract_text(layout=True) or ""
                low = t.lower()
                if any(k in low for k in ("relative importance", "first-time, first-year", "first-time/first-year",
                                          "admission", "sat ", "act ", "75th", "rigor", "freshman profile", "c7", "c9", "c1")):
                    chunks.append(t)
        os.unlink(tf.name)
        text = "\n".join(chunks)
        return text[:22000] if len(text) > 300 else None
    except Exception:
        return None


def extract(name, text, claude) -> tuple[dict | None, dict]:
    r = claude.messages.create(
        model=SONNET, max_tokens=1200,
        system=[{"type": "text", "text": EXTRACT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"College: {name}\n\nCDS text:\n```\n{text}\n```"}],
    )
    out = "".join(b.text for b in r.content if hasattr(b, "text"))
    usage = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
    m = re.search(r"\{[\s\S]*\}", out)
    try:
        return (json.loads(m.group()) if m else None), usage
    except Exception:
        return None, usage


def process(slug, name, claude) -> dict:
    rec = {"slug": slug, "name": name, "ts": time.time(), "usage": {}}
    url, u1 = find_url(name, claude)
    rec["usage"]["find"] = u1
    if not url:
        rec["status"] = "no_url"; log(f"  {slug}: no URL found"); return rec
    rec["url"] = url
    text = get_pdf_text(url)
    if not text:
        rec["status"] = "pdf_fail"; log(f"  {slug}: PDF download/parse failed ({url[:60]})"); return rec
    d, u2 = extract(name, text, claude)
    rec["usage"]["extract"] = u2
    if not d:
        rec["status"] = "no_json"; log(f"  {slug}: extraction returned no json"); return rec
    d["slug"] = slug
    rec["extracted"] = d; rec["status"] = "ok"
    c7 = sum(1 for v in (d.get("c7_factors") or {}).values() if v)
    log(f"  ok {slug}: {d.get('cds_year')} accept={d.get('accept')} conf={d.get('confidence')} c7={c7}/18")
    return rec


def main():
    key = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key: print("ANTHROPIC_KEY not set", file=sys.stderr); sys.exit(1)
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else list(TARGETS)
    claude = anthropic.Anthropic(api_key=key)
    log(f"start: {len(only)} schools (cheap download+parse path)")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(process, s, TARGETS[s], claude): s for s in only if s in TARGETS}
        n = 0
        for fut in as_completed(futs):
            rec = fut.result()
            with open(RESULTS, "a") as f: f.write(json.dumps(rec) + "\n")
            n += 1
            if n % 5 == 0 or n == len(futs): log(f"progress: {n}/{len(futs)}")
    # cost tally (Haiku $1/$5 + Sonnet $3/$15 + ws $10/1k)
    hin=hout=ws=sin=sout=0
    for l in RESULTS.read_text().splitlines():
        try: u=json.loads(l).get("usage") or {}
        except: continue
        f=u.get("find") or {}; e=u.get("extract") or {}
        hin+=f.get("in",0); hout+=f.get("out",0); ws+=f.get("ws",0)
        sin+=e.get("in",0); sout+=e.get("out",0)
    cost = hin/1e6*1 + hout/1e6*5 + sin/1e6*3 + sout/1e6*15 + ws/1000*10
    log(f"DONE. haiku_in={hin} sonnet_in={sin} sonnet_out={sout} websearches={ws}  est_cost=${cost:.2f}")


if __name__ == "__main__":
    main()
