"""LIVE benchmark of the OLD business_collector.py on the same task the app
is benchmarked on (one category, one country). COSTS SERPER CREDITS.

The old script's own query style and logic are kept (base phrases, 2 Places
pages per query, organic + 60-link directory mining); only its scope is
narrowed from "worldwide, 10 categories" to "<category> in <country> and its
major cities", so both versions search the same market. Measured from the
outside: every Serper response is metered, billed credits come from the
account balance, and the old records are re-checked with the APP's
validator + dedup registry so "valid records" means the same thing.

    python tests/bench_old_script.py --category Recruitment --target 500
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(APP)


def _dom(u: str) -> str:
    n = urlparse(u or "").netloc.lower().split(":")[0]
    return n[4:] if n.startswith("www.") else n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="Recruitment")
    ap.add_argument("--country", default="USA")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--label", default="OLD")
    a = ap.parse_args()

    for line in open(os.path.join(APP, ".env"), encoding="utf-8-sig"):
        k, _, v = line.strip().partition("=")
        if k == "SERPER_API_KEY":
            os.environ["SERPER_API_KEY"] = v.split(" #")[0].strip().strip('"').strip("'")
    sys.path.insert(0, ROOT)
    sys.path.insert(0, APP)
    import requests
    import business_collector as bc
    from backend.collector.categories import cities_for
    from backend.collector.deduplicator import DedupRegistry
    from backend.collector.validator import validate

    # -- same market as the app: <phrases> in <country>, then its cities ------
    phrases = bc.CATEGORIES[a.category]
    bc.CATEGORIES.clear()
    bc.CATEGORIES[a.category] = phrases
    mods = [f"in {a.country}"] + [f"in {c}" for c in cities_for(a.country)]
    bc.CategoryState._plan = staticmethod(
        lambda queries, _exp: (f"{q} {m}" for m in mods for q in queries))

    # -- meter every Serper response ------------------------------------------
    meter = {"places": 0, "organic": 0, "results": 0, "credits": 0}
    seen: set[str] = set()
    new_ids = [0]
    lock = threading.Lock()
    orig_post = bc.Serper._post

    def post(self, url, payload):
        data = orig_post(self, url, payload)
        kind = "places" if url.endswith("/places") else "organic"
        ids = ([_dom(p.get("website", "")) or f"cid:{p.get('cid')}" for p in data.get("places") or []]
               if kind == "places" else
               [_dom(o.get("link", "")) for o in data.get("organic") or []])
        with lock:
            meter[kind] += 1
            meter["credits"] += int(data.get("credits", 1) or 1)
            meter["results"] += len(ids)
            fresh = [i for i in dict.fromkeys(ids) if i not in seen]
            new_ids[0] += len(fresh)
            seen.update(fresh)
        return data
    bc.Serper._post = post

    def balance():
        try:
            r = requests.get("https://google.serper.dev/account", timeout=15,
                             headers={"X-API-KEY": os.environ["SERPER_API_KEY"]})
            return int(r.json()["balance"])
        except Exception:
            return None

    out = tempfile.mkdtemp(prefix="bench_old_")
    b0 = balance()
    if b0 is not None and b0 <= 0:
        print(json.dumps({"label": a.label, "error": f"balance {b0}"}))
        return
    t0 = time.perf_counter()
    bc.main(["--target", str(a.target), "--fresh", "--out", out, "--engine", "serper"])
    wall = time.perf_counter() - t0
    time.sleep(3)
    b1 = balance()

    st = json.load(open(os.path.join(out, "collector_state.json"), encoding="utf-8"))
    recs = st["records"]
    reg = DedupRegistry()
    valid = 0
    for r in recs:   # the app's rules, applied to the old records
        rec = {"Company Name": r.get("Company Name", ""),
               "Official Website": r.get("Website", ""),
               "Business Email": r.get("Email", ""), "Business Phone": r.get("Phone", ""),
               "Full Business Address": r.get("Address", ""), "_cid": r.get("_cid", "")}
        ok, _ = validate(rec, from_places=bool(r.get("_cid")))
        if ok and reg.check_and_add(rec, a.category)[0]:
            valid += 1
    billed = (b0 - b1) if b0 is not None and b1 is not None else None
    cr = billed if billed is not None else meter["credits"]
    res = {
        "label": a.label, "category": a.category, "target": a.target,
        "records_old_rules": len(recs), "valid_records": valid,
        "credits_billed": billed, "credits_metered": meter["credits"],
        "places_calls": meter["places"], "organic_calls": meter["organic"],
        "search_results": meter["results"], "unique_domains": new_ids[0],
        "duplicate_results": meter["results"] - new_ids[0],
        "valid_per_credit": round(valid / cr, 2) if cr else None,
        "credits_per_valid": round(cr / valid, 3) if valid else None,
        "runtime_s": round(wall, 1),
        "queries": len(st.get("query_log", [])),
    }
    print("RESULT " + json.dumps(res), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
