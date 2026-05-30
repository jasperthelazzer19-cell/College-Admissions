"""Crawl candoradmit.com for broken links — sample-based.

Strategy:
  1. Fetch homepage, extract every internal href.
  2. Bucket URLs by route pattern (/college/<slug>, /rankings/<slug>, etc).
  3. Test all top-level routes individually AND 5 sampled URLs per pattern.
  4. Pace requests to stay below the app's rate limit.
"""
import re
import sys
import time
from collections import defaultdict
import httpx

BASE = "https://candoradmit.com"
DELAY = 1.2   # seconds between requests
HREF_RE = re.compile(r'href="([^"]+)"')


def normalize(href: str) -> str | None:
    if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
        return None
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("http"):
        return href.split("#")[0] if href.startswith(BASE) else None
    if href.startswith("/"):
        return BASE + href.split("#")[0]
    return None


def pattern_of(url: str) -> str:
    path = url[len(BASE):]
    # Normalize: /college/<slug> -> /college/SLUG
    parts = path.split("/")
    return "/".join("SLUG" if (i > 1 and len(parts[i]) > 0) else parts[i] for i in range(len(parts)))


def fetch(url: str, client: httpx.Client) -> int:
    try:
        r = client.get(url)
        return r.status_code
    except Exception:
        return -1


def main():
    client = httpx.Client(follow_redirects=True, timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (audit; +candor)"})
    # Discover URLs from homepage + a school detail page (richest link source)
    discovered: set[str] = set()
    for seed in ["/", "/colleges", "/rankings", "/college/harvard", "/college/vanderbilt", "/profiles", "/upgrade"]:
        try:
            r = client.get(BASE + seed)
            for m in HREF_RE.finditer(r.text):
                u = normalize(m.group(1))
                if u: discovered.add(u)
        except Exception as e:
            print(f"seed error {seed}: {e}")
        time.sleep(DELAY)
    print(f"discovered {len(discovered)} unique internal URLs")

    # Bucket
    buckets: dict[str, list[str]] = defaultdict(list)
    for u in discovered:
        buckets[pattern_of(u)].append(u)
    print(f"unique URL patterns: {len(buckets)}\n")
    for p, urls in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"  {p:<45} {len(urls)} URLs")
    print()

    # For each pattern, test up to 5 URLs
    failures = []
    print("\n--- testing 5 URLs per pattern ---\n")
    for pat, urls in sorted(buckets.items()):
        for u in urls[:5]:
            code = fetch(u, client)
            time.sleep(DELAY)
            if code != 200:
                failures.append((u, code))
                print(f"  {code}  {u}")

    print(f"\n\n{'='*60}")
    print(f"non-200 endpoints: {len(failures)}")
    for u, c in sorted(failures, key=lambda x: x[1]):
        print(f"  {c}  {u}")


if __name__ == "__main__":
    main()
