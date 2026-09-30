"""LIVE end-to-end benchmark against the real Serper API (costs credits).

Runs one collection in-process on a FRESH temporary state (no cache, empty
dedup registry), so repeated runs spend the same searches on the same sites
and before/after numbers are directly comparable. Your real data in
app/output is never touched.

Usage (from app/):
    python tests/bench_live.py [target] [category] [country] [keywords]
    python tests/bench_live.py 200 Advisory USA "management consulting"
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402  (loads app/.env)

tmp = tempfile.mkdtemp(prefix="bench_live_")
config.OUTPUT_DIR = tmp
config.STATE_PATH = os.path.join(tmp, "state.json")

from backend.collector.engine import CollectionJob, StateStore  # noqa: E402

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 200
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "Advisory"
COUNTRY = sys.argv[3] if len(sys.argv) > 3 else "USA"
KEYWORDS = [k.strip() for k in (sys.argv[4] if len(sys.argv) > 4
                                else "management consulting").split(",")]
MAX_QUERIES = min(config.MAX_MAX_QUERIES, max(12, TARGET // 4))

logging.basicConfig(level=logging.INFO, filename=os.path.join(tmp, "bench.log"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def main():
    if not config.serper_api_key():
        sys.exit("SERPER_API_KEY missing in app/.env")
    st = StateStore()
    job = CollectionJob(st, CATEGORY, KEYWORDS, COUNTRY, TARGET, "serper",
                        MAX_QUERIES, geo={"country": COUNTRY})
    print(f"LIVE BENCH: {CATEGORY} / {COUNTRY} / {KEYWORDS} target={TARGET} "
          f"max_queries={MAX_QUERIES} workers={config.CRAWL_WORKERS}")
    t0 = time.perf_counter()
    job._run()
    wall = time.perf_counter() - t0
    c = job.counters
    timings = job.timings.snapshot()
    perf = job.perf() if hasattr(job, "perf") else {}
    valid = len(st.records[CATEGORY])
    credits = c.get("serper_credits", 0)
    fetch = timings.get("fetch", {})
    lat = sorted(d for d, _, _ in getattr(job, "_latencies", []))
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0
    out = {
        "status": job.status,
        "wall_s": round(wall, 1),
        "valid_records": valid,
        "records_per_min": round(60 * valid / wall, 1),
        "serper_credits": credits,
        "records_per_credit": round(valid / credits, 2) if credits else None,
        "serper_requests": c.get("search_requests", 0),
        "serper_req_per_min": round(60 * c.get("search_requests", 0) / wall, 1),
        "serper_avg_ms": round(1000 * sum(lat) / len(lat)) if lat else 0,
        "serper_p95_ms": round(1000 * p95),
        "serper_429": c.get("serper_429", 0),
        "queries_executed": c.get("queries_executed", 0),
        "websites_fetched": fetch.get("count", 0),
        "fetches_per_min": round(60 * fetch.get("count", 0) / wall, 1),
        "avg_fetch_ms": fetch.get("avg_ms", 0),
        "duplicates": c.get("duplicates", 0),
        "duplicate_rate": round(c.get("duplicates", 0) / max(1, c.get("discovered", 0)), 3),
        "failed_urls": c.get("failed", 0),
        "timings": timings,
        "perf": perf,
    }
    print(json.dumps(out, indent=2))
    print(f"log: {os.path.join(tmp, 'bench.log')}")


if __name__ == "__main__":
    main()
