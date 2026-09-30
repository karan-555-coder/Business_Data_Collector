"""OFFLINE credit A/B benchmark (no network, no Serper credits).

Runs the REAL engine of any backend copy (--root) against a simulated Serper
whose behaviour is CALIBRATED to the live request log of 2026-09-29/30
(1,235 Serper requests, app/output/app.log*):

    Places page 1   ~6 new businesses per 10 results (34% repeats)
    Places page 2   ~1/3 as novel as page 1 (79% repeats)
    organic         ~63% repeats; 1-3 of 10 links are directories / social /
                    news (mined or skipped, never records)
    ~40% of Places listings have no website

Businesses of a location are shared by every phrase searched there (popular
firms show up for most phrases), which is where real duplicates come from.
The numbers this prints are a SIMULATION of identical conditions for both
versions - the live proof is tests/bench_discovery.py (costs credits).

    python tests/bench_credits_offline.py --root <dir with backend/> \
        --category Recruitment --target 500
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Calibrated against the baseline engine (Recruitment, target 500): page-1
# repeats 35% (live 34%), organic 53% (live 63%), page 2 49% (live 79% -
# so this sim UNDER-states what skipping page-2 saves), 4.7 valid records
# per credit (live 4.2-5.0 on fresh categories).
GEO_POOL = 30           # businesses reachable per location
HEAD = 12               # "popular" businesses per location
NATIONAL = 120          # firms that rank in every location ...
NATIONAL_P = 0.45       # ... and fill this share of any result list
DIRECTORIES = ["https://www.yelp.com/search?find_desc=x", "https://clutch.co/list",
               "https://www.linkedin.com/company/some-firm",
               "https://www.indeed.com/cmp/x", "https://en.wikipedia.org/wiki/X"]


def h(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class World:
    def biz(self, i: int) -> dict:
        dom = f"firm{i}.example"
        return {"id": i, "name": f"Firm {i} {['Partners', 'Group', 'Associates', 'Co'][i % 4]}"
                f" {chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}",
                "domain": dom, "has_site": h("site", i) % 10 < 6,
                "phone": f"+1 {4000000000 + i}", "email": f"hello@{dom}",
                "address": f"{100 + i % 800} Market Street, Austin, Texas 78701, USA"}

    @staticmethod
    def _geo(query: str) -> str:
        return query.rsplit(" in ", 1)[1].lower() if " in " in query else query.lower()

    def ids(self, kind: str, query: str, page: int) -> list[int]:
        geo = self._geo(query)
        base = (h("geo", geo) % 50_000) * GEO_POOL
        rng = random.Random(h(kind, query.lower(), page))
        out: list[int] = []
        seen: set[int] = set()
        # page 1: 35% popular head; page 2+: 80% head (calibrated repeats);
        # organic: 60% head
        head_p = 0.35 if (kind == "places" and page == 1) else 1.0 if kind == "places" else 0.8
        while len(out) < 10:
            if rng.random() < NATIONAL_P:      # national firms rank everywhere
                i = 90_000_000 + rng.randrange(NATIONAL)
            else:
                k = (rng.randrange(HEAD) if rng.random() < head_p
                     else HEAD + rng.randrange(GEO_POOL - HEAD))
                i = base + k
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=APP_DIR)
    ap.add_argument("--category", default="Recruitment")
    ap.add_argument("--custom", default="", help="custom category display name")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--country", default="USA")
    ap.add_argument("--label", default="run")
    ap.add_argument("--pool", type=int, default=GEO_POOL)
    ap.add_argument("--head", type=int, default=HEAD)
    ap.add_argument("--national", type=int, default=NATIONAL)
    ap.add_argument("--national-p", type=float, default=NATIONAL_P)
    a = ap.parse_args()
    globals().update(GEO_POOL=a.pool, HEAD=a.head, NATIONAL=a.national,
                     NATIONAL_P=a.national_p)

    sys.path.insert(0, os.path.abspath(a.root))
    from backend import config
    tmp = tempfile.mkdtemp(prefix="bench_credits_")
    config.OUTPUT_DIR = tmp
    config.STATE_PATH = os.path.join(tmp, "state.json")
    config.ANALYZE_PROCESSES = 0
    config.MAX_CREDITS_PER_RECORD = 0      # both versions may spend what they need
    from backend.collector import engine
    from backend.collector.search import SearchProvider

    world = World()

    class SimSerper(SearchProvider):
        name = "sim"
        supports_places = True

        def __init__(self):
            self.lock = threading.Lock()
            self.credits_used = self.ok_count = self.fail_count = 0
            self.calls = {"places": 0, "organic": 0}
            self.results = 0
            self.seen: set[str] = set()
            self.new = 0
            self.by: dict[str, list] = {}   # kind+page -> [calls, results, new]

        @property
        def requests_made(self):
            return sum(self.calls.values())

        def _bill(self, kind, idents, page=1):
            time.sleep(random.uniform(0.02, 0.06))
            with self.lock:
                self.calls[kind] += 1
                self.credits_used += 1
                self.ok_count += 1
                self.results += len(idents)
                fresh = [i for i in dict.fromkeys(idents) if i not in self.seen]
                self.new += len(fresh)
                self.seen.update(fresh)
                s = self.by.setdefault(f"{kind}{page}", [0, 0, 0])
                s[0] += 1
                s[1] += len(idents)
                s[2] += len(fresh)

        def organic(self, query, max_results):
            return self.organic_page(query, 1)

        def organic_page(self, query, page):
            ids = world.ids("organic", query, page)
            ndir = 1 + h("dir", query, page) % 3
            urls = [f"https://{world.biz(i)['domain']}/" for i in ids[:10 - ndir]
                    if world.biz(i)["has_site"]]
            urls += DIRECTORIES[:ndir]
            self._bill("organic", [u.split("/")[2] for u in urls], page)
            return urls

        def places_page(self, query, page):
            rows = []
            for i in world.ids("places", query, page):
                b = world.biz(i)
                row = {"title": b["name"], "phoneNumber": b["phone"],
                       "cid": str(7_000_000 + i), "address": b["address"],
                       "category": "Business"}
                if b["has_site"]:
                    row["website"] = f"https://{b['domain']}"
                rows.append(row)
            self._bill("places", [r.get("website", "").split("/")[-1] or r["cid"] for r in rows],
                       page)
            return rows

    class SimFetcher:
        def fetch_raw(self, url):
            time.sleep(random.uniform(0.01, 0.04))
            host = url.split("/")[2]
            digits = "".join(ch for ch in host.split(".")[0] if ch.isdigit())
            if not digits:
                return None, url, "connection/DNS error"
            b = world.biz(int(digits))
            if int(digits) % 11 == 0:       # some sites are down
                return None, url, "connection/DNS error"
            html = (f"<html><head><title>{b['name']}</title></head><body>"
                    f"<h1>{b['name']}</h1><a href='mailto:{b['email']}'>Email</a> "
                    f"<a href='tel:{b['phone']}'>Call</a><address>{b['address']}"
                    f"</address><p>Professional services for businesses.</p></body></html>")
            return html.encode(), url, ""

    prov = SimSerper()
    engine.make_provider = lambda *_: prov
    st = engine.StateStore()
    cat = st.ensure_custom(a.custom) if a.custom else a.category
    kws = [a.custom] if a.custom else []
    job = engine.CollectionJob(st, cat, kws, a.country, a.target, "serper", 12,
                               geo={"country": a.country})
    job.fetcher = SimFetcher()
    t0 = time.perf_counter()
    job._run()
    wall = time.perf_counter() - t0
    recs = st.records[cat]
    doms = [r["Official Website"] for r in recs if r.get("Official Website")]
    valid = len(recs)
    cr = prov.credits_used
    out = {
        "label": a.label, "category": a.custom or a.category, "target": a.target,
        "status": job.status, "valid_records": valid,
        "duplicate_domains_stored": len(doms) - len(set(doms)),
        "credits": cr, "places_calls": prov.calls["places"],
        "organic_calls": prov.calls["organic"],
        "search_results": prov.results, "duplicate_results": prov.results - prov.new,
        "unique_domains": prov.new,
        "valid_per_credit": round(valid / cr, 2) if cr else None,
        "runtime_s": round(wall, 1),
        "dup_pct_by_kind_page": {k: [v[0], round(100 * (v[1] - v[2]) / v[1]) if v[1] else None]
                                 for k, v in sorted(prov.by.items())},
    }
    if hasattr(job, "credit_metrics"):   # newer engines explain their spend
        out["engine_credit_metrics"] = job.credit_metrics()
        ds = job.planner.summary()
        out["planner"] = {k: ds.get(k) for k in (
            "cells_issued", "followups_issued", "followup_novelty_ratio",
            "thin_places_organic", "organic_not_paired", "kind_efficiency", "tier")}
    print("RESULT " + json.dumps(out), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
