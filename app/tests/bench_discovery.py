"""LIVE discovery benchmark against the real Serper API (costs credits).

Measures the pipeline from the OUTSIDE (wraps the Serper HTTP session and
the page fetcher), so any engine version - old or new - is measured the
same way. Runs on a FRESH temporary state (empty cache + dedup registry);
app/output is never touched.

Usage (from app/):
    python tests/bench_discovery.py --target 100 --max-queries 12 \
        --category Advisory --country USA --keywords "management consulting"
    # measure another copy of the backend (e.g. a pre-change backup):
    python tests/bench_discovery.py --root C:/path/containing/backend ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env():
    path = os.path.join(APP_DIR, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _dom(url: str) -> str:
    net = urlparse(url or "").netloc.lower().split(":")[0]
    return net[4:] if net.startswith("www.") else net


class Meter:
    """Thread-safe counters for Serper traffic and crawl fetches."""

    def __init__(self):
        self.lock = threading.Lock()
        self.serper_lat: list[float] = []
        self.serper_status: dict = {}
        self.per_request: list[dict] = []   # {kind, q, results, new}
        self.seen_ids: set[str] = set()
        self.credits = 0
        self.fetches = 0
        self.fetch_s = 0.0

    def serper(self, url: str, payload: dict, dt: float, status, data: dict):
        kind = "places" if url.endswith("/places") else "organic"
        ids: list[str] = []
        if kind == "places":
            for p in data.get("places") or []:
                ids.append(_dom(p.get("website") or "")
                           or f"cid:{p.get('cid') or p.get('placeId') or p.get('title')}")
        else:
            for o in data.get("organic") or []:
                if (o.get("link") or "").startswith("http"):
                    ids.append(_dom(o["link"]))
        with self.lock:
            if status == 200:
                self.credits += int(data.get("credits", 1) or 1)
            self.serper_lat.append(dt)
            self.serper_status[status] = self.serper_status.get(status, 0) + 1
            uniq_in_req = list(dict.fromkeys(ids))
            new = [i for i in uniq_in_req if i not in self.seen_ids]
            self.seen_ids.update(uniq_in_req)
            self.per_request.append({"kind": kind, "q": payload.get("q"),
                                     "page": payload.get("page", 1),
                                     "results": len(ids), "new": len(new)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=APP_DIR, help="dir containing backend/")
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--max-queries", type=int, default=12)
    ap.add_argument("--category", default="Advisory")
    ap.add_argument("--country", default="USA")
    ap.add_argument("--state", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--keywords", default="management consulting")
    ap.add_argument("--custom", default="", help="custom category display name")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    _load_env()
    sys.path.insert(0, os.path.abspath(a.root))
    from backend import config
    tmp = tempfile.mkdtemp(prefix="bench_disc_")
    config.OUTPUT_DIR = tmp
    config.STATE_PATH = os.path.join(tmp, "state.json")
    logging.basicConfig(level=logging.INFO, filename=os.path.join(tmp, "bench.log"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    from backend.collector import engine
    from backend.collector import crawler

    meter = Meter()

    # -- wrap the Serper HTTP session (HTTP-level latency, results) ----------
    orig_make = engine.make_provider

    def make_provider(name, key):
        prov = orig_make(name, key)
        orig_post = prov.session.post

        def post(url, json=None, **kw):
            t0 = time.perf_counter()
            status, data = "ERR", {}
            try:
                resp = orig_post(url, json=json, **kw)
                status = resp.status_code
                if status == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        pass
                return resp
            finally:
                meter.serper(url, json or {}, time.perf_counter() - t0, status, data)
        prov.session.post = post
        return prov
    engine.make_provider = make_provider

    # -- count every website page fetch (robots.txt excluded) ----------------
    orig_fetch = crawler.Fetcher.fetch

    def fetch(self, url, *args, **kw):
        t0 = time.perf_counter()
        try:
            return orig_fetch(self, url, *args, **kw)
        finally:
            with meter.lock:
                meter.fetches += 1
                meter.fetch_s += time.perf_counter() - t0
    crawler.Fetcher.fetch = fetch

    import requests as _rq

    def balance():
        """Serper's own account balance (free endpoint): the billed truth,
        including timed-out requests Serper may have charged."""
        try:
            r = _rq.get("https://google.serper.dev/account", timeout=15,
                        headers={"X-API-KEY": os.environ.get("SERPER_API_KEY", "")})
            return int(r.json().get("balance")) if r.status_code == 200 else None
        except Exception:
            return None
    bal_before = balance()
    if bal_before is not None and bal_before <= 0:
        print(json.dumps({"label": a.label, "error": f"Serper balance {bal_before}: "
                          "top up before a live benchmark"}), flush=True)
        os._exit(2)

    st = engine.StateStore()
    if a.custom:
        a.category = st.ensure_custom(a.custom)
    geo = {"city": a.city, "state": a.state, "country": a.country}
    location = ", ".join(p for p in (a.city, a.state, a.country) if p)   # as main.py
    job = engine.CollectionJob(st, a.category, [k.strip() for k in a.keywords.split(",")],
                               location, a.target, "serper", a.max_queries, geo=geo)
    print(f"[{a.label}] {a.category}/{location}/{a.keywords} target={a.target} "
          f"max_queries={a.max_queries} root={a.root}", flush=True)
    t0 = time.perf_counter()
    t_target = None
    done = threading.Event()

    def watch():   # time-to-target, independent of the engine's own clock
        nonlocal t_target
        while not done.is_set():
            if t_target is None and len(st.records[a.category]) >= a.target:
                t_target = time.perf_counter() - t0
            time.sleep(0.2)
    threading.Thread(target=watch, daemon=True).start()
    job._run()
    wall = time.perf_counter() - t0
    done.set()

    c = job.counters
    valid = len(st.records[a.category])
    credits = meter.credits   # from Serper's own per-response credit field
    time.sleep(3)             # let the account endpoint catch up
    bal_after = balance()
    reqs = len(meter.per_request)
    res_total = sum(r["results"] for r in meter.per_request)
    new_total = sum(r["new"] for r in meter.per_request)
    by_kind = {}
    for k in ("organic", "places"):
        rs = [r for r in meter.per_request if r["kind"] == k]
        if rs:
            by_kind[k] = {"requests": len(rs),
                          "results": sum(r["results"] for r in rs),
                          "new_unique": sum(r["new"] for r in rs)}
    lat = sorted(meter.serper_lat)
    out = {
        "label": a.label,
        "status": job.status,
        "valid_records": valid,
        "target_reached": valid >= a.target,
        "total_runtime_s": round(wall, 1),
        "time_to_target_s": round(t_target, 1) if t_target else None,
        "credits_consumed": credits,
        "credits_billed_balance_delta": (bal_before - bal_after)
        if bal_before is not None and bal_after is not None else None,
        "valid_per_billed_credit": round(valid / (bal_before - bal_after), 2)
        if bal_before is not None and bal_after is not None and bal_before > bal_after
        else None,
        "unique_domains": len(meter.seen_ids),
        "duplicate_results": res_total - new_total,
        "serper_http_requests": sum(meter.serper_status.values()),
        "serper_status_counts": {str(k): v for k, v in meter.serper_status.items()},
        "serper_requests_per_min": round(60 * sum(meter.serper_status.values()) / wall, 1),
        "serper_avg_latency_ms": round(1000 * sum(lat) / len(lat)) if lat else 0,
        "serper_p95_latency_ms": round(1000 * lat[int(0.95 * (len(lat) - 1))]) if lat else 0,
        "unique_domains_per_query": round(new_total / reqs, 2) if reqs else 0,
        "result_duplicate_pct": round(100 * (res_total - new_total) / res_total, 1) if res_total else 0,
        "record_duplicate_pct": round(100 * c.get("duplicates", 0) / max(1, c.get("discovered", 0)), 1),
        "valid_records_per_credit": round(valid / credits, 2) if credits else None,
        "crawl_requests": meter.fetches,
        "crawl_requests_per_min": round(60 * meter.fetches / wall, 1),
        "avg_crawl_latency_ms": round(1000 * meter.fetch_s / meter.fetches) if meter.fetches else 0,
        "valid_records_per_min": round(60 * valid / wall, 1),
        "queries_executed": c.get("queries_executed", 0),
        "by_kind": by_kind,
        "engine_counters": {k: c.get(k) for k in (
            "candidate_urls", "tasks_done", "places_followups", "searches_done",
            "new_results", "skipped_known", "failed") if k in c},
        "per_request": meter.per_request,
        "log": os.path.join(tmp, "bench.log"),
    }
    if hasattr(job, "credit_metrics"):
        out["engine_credit_metrics"] = job.credit_metrics()
    summary = {k: v for k, v in out.items() if k != "per_request"}
    print(json.dumps(summary, indent=2), flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
    from backend.collector import analysis
    if analysis._pool is not None:        # else its workers outlive us
        analysis._pool.shutdown(wait=True, cancel_futures=True)
    os._exit(0)   # don't wait on stray background crawl threads


if __name__ == "__main__":
    main()
