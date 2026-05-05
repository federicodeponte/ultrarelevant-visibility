#!/usr/bin/env python3
"""Regenerate cache for all 9 companies under new 3-dimension methodology."""
import json
import time
import sys
import requests
from pathlib import Path

ENGINE = "https://visibility.fedeponte.com"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

COMPANIES = [
    {"slug": "bosch-rexroth", "display_name": "Bosch Rexroth", "url": "https://boschrexroth.com"},
    {"slug": "siemens", "display_name": "Siemens", "url": "https://siemens.com"},
    {"slug": "abb", "display_name": "ABB", "url": "https://abb.com"},
    {"slug": "festo", "display_name": "Festo", "url": "https://festo.com"},
    {"slug": "igus", "display_name": "igus", "url": "https://igus.de"},
    {"slug": "daw", "display_name": "DAW", "url": "https://daw.de"},
    {"slug": "schneider-electric", "display_name": "Schneider Electric", "url": "https://schneider-electric.com"},
    {"slug": "basf", "display_name": "BASF", "url": "https://basf.com"},
    {"slug": "skf", "display_name": "SKF", "url": "https://skf.com"},
]

def start_job(company):
    r = requests.post(f"{ENGINE}/analyze", json={"url": company["url"]}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d["job_id"]

def poll_job(job_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{ENGINE}/status/{job_id}", timeout=15)
        r.raise_for_status()
        d = r.json()
        if d["status"] == "complete":
            return d["result"]
        if d["status"] == "error":
            raise RuntimeError(f"job {job_id} failed: {d.get('error')}")
        print(f"  [{job_id[:12]}] {d.get('progress','...')} ({d.get('elapsed',0):.0f}s)", flush=True)
        time.sleep(5)
    raise TimeoutError(f"job {job_id} timed out after {timeout}s")

def main():
    # Start all jobs in parallel
    jobs = {}
    for c in COMPANIES:
        try:
            job_id = start_job(c)
            jobs[job_id] = c
            print(f"Started {c['slug']} -> {job_id}", flush=True)
        except Exception as e:
            print(f"ERROR starting {c['slug']}: {e}", file=sys.stderr)

    # Poll until all complete
    results = {}
    for job_id, c in jobs.items():
        print(f"\nPolling {c['slug']} ({job_id[:12]})...", flush=True)
        try:
            result = poll_job(job_id, timeout=360)
            results[c["slug"]] = result
            print(f"  Done: found={result.get('found_score')} honest={result.get('honest_score')} sourced={result.get('sourced_score')} trust={result.get('trust_score')}", flush=True)
        except Exception as e:
            print(f"ERROR polling {c['slug']}: {e}", file=sys.stderr)

    # Save to cache
    print("\nSaving cache files...", flush=True)
    for c in COMPANIES:
        slug = c["slug"]
        if slug not in results:
            print(f"  SKIP {slug} (no result)", file=sys.stderr)
            continue
        result = results[slug]
        result["slug"] = slug
        result["display_name"] = c["display_name"]
        out = CACHE_DIR / f"{slug}.json"
        out.write_text(json.dumps(result, indent=2))
        print(f"  Saved {out}", flush=True)

    # Print summary table
    print("\n=== Score Table ===")
    print(f"{'slug':<24} {'found':>6} {'honest':>7} {'sourced':>8} {'trust':>6}")
    print("-" * 56)
    for c in COMPANIES:
        slug = c["slug"]
        if slug in results:
            r = results[slug]
            print(f"{slug:<24} {r.get('found_score',0):>6} {r.get('honest_score',0):>7} {r.get('sourced_score',0):>8} {r.get('trust_score',0):>6}")
        else:
            print(f"{slug:<24} {'FAILED':>6}")

if __name__ == "__main__":
    main()
