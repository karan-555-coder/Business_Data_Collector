"""Credit-guard tests (offline, no Serper credits):

  * target already reached        -> zero API calls
  * same normalized query again   -> served from cache, zero credits
  * executed + cache expired       -> skipped via the persistent registry,
    also after a restart from the on-disk checkpoint and from another category
  * query normalization            ("Top ... agencies in St. Louis" == "... agency in St Louis")
  * a TIMED-OUT request is re-sent at most SERPER_TIMEOUT_RETRIES times
  * per-query ledger: credits, results, new, valid attributed
  * category delete purges its registry entries (re-collect possible)

Run (from app/):  python tests/test_credits.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="credits_test_")
config.OUTPUT_DIR = _tmp
config.STATE_PATH = os.path.join(_tmp, "state.json")
config.ANALYZE_PROCESSES = 0

import requests  # noqa: E402

from backend.collector import engine  # noqa: E402
from backend.collector.credits import ledger_key, norm_query  # noqa: E402
from backend.collector.engine import CollectionJob, StateStore  # noqa: E402
from backend.collector.search import SearchError, SearchProvider, SerperProvider  # noqa: E402


class CountingProvider(SearchProvider):
    name = "count"
    supports_places = True

    def __init__(self):
        self.calls: list[tuple] = []
        self.credits_used = self.ok_count = self.fail_count = 0

    @property
    def requests_made(self):
        return len(self.calls)

    def _hit(self, kind, q, page):
        self.calls.append((kind, q, page))
        self.credits_used += 1

    def organic(self, query, max_results):
        self._hit("organic", query, 1)
        n = len(self.calls)
        return [f"https://firm{n}{k}.example/" for k in range(5)]

    def organic_page(self, query, page):
        self._hit("organic", query, page)
        return []

    def places_page(self, query, page):
        self._hit("places", query, page)
        n = len(self.calls)
        return [{"title": f"Places Firm {n} {k} Holdings", "cid": str(n * 100 + k),
                 "phoneNumber": f"+1 555 {n:03d} {k:04d}",
                 "address": "1 Main St, Austin, Texas 78701, USA"} for k in range(10)]


class Fetcher:
    def fetch_raw(self, url):
        return None, url, "offline"


def new_job(state, category="Recruitment", target=5):
    job = CollectionJob(state, category, [], "USA", target, "serper", 4,
                        geo={"country": "USA"})
    job.fetcher = Fetcher()
    return job


def main():
    # 1. normalization: rephrasings / punctuation / order collapse to one key
    assert norm_query("Top recruitment agencies in St. Louis") == \
        norm_query("recruitment agency in St Louis"), "rephrasing not normalized"
    assert norm_query("staffing agency in Delhi") != norm_query("recruitment agency in Delhi")
    assert ledger_key("places", "IT  Staffing Companies in Noida") == \
        ledger_key("places", "it staffing company in noida")
    print("normalization OK")

    # 2. a run records every paid search in the ledger
    prov = CountingProvider()
    engine.make_provider = lambda *_: prov
    st = StateStore()
    job = new_job(st, target=5)
    job._run()
    assert job.status == "completed", job.stop_reason
    paid = len(prov.calls)
    led = st.ledger.summary()
    assert led["total_credits"] == paid and led["valid_records"] >= 5, led
    m = job.credit_metrics()
    assert m["total_credits"] == paid and m["valid_businesses"] == 5, m
    assert os.path.exists(os.path.join(_tmp, "credit_ledger.csv"))
    print(f"ledger OK ({paid} credits, {led['valid_records']} valid attributed)")

    # 3. target already reached -> not a single API call
    prov.calls.clear()
    job = new_job(st, target=5)
    job._run()
    assert job.status == "completed" and prov.calls == [], prov.calls
    print("target reached -> 0 calls OK")

    # 4. same queries again: cache hit (0 credits); cache gone: registry skip
    executed = [ledger_key(*c) for c in list(st.ledger.entries)[:0]] or \
        [k for k, e in st.ledger.entries.items() if e["credits"] > 0]
    kind, q = st.ledger.entries[executed[0]]["kind"], st.ledger.entries[executed[0]]["query"]
    job = new_job(st, target=50)
    job.provider = prov
    prov.calls.clear()
    res, src, cr = job._timed_search(kind, q.replace(" in ", "  in  ").upper(), 1)
    assert src == "cache" and cr == 0 and prov.calls == [], (src, prov.calls)
    st.search_cache.clear()
    res, src, cr = job._timed_search(kind, q, 1)
    assert src == "executed" and cr == 0 and prov.calls == [], (src, prov.calls)
    print("repeat query -> cache / executed-registry skip, 0 credits OK")

    # 5. persisted: restart from the checkpoint, other category, cache expired
    st.save(config.STATE_PATH)
    st2 = StateStore.load(config.STATE_PATH)
    st2.search_cache.clear()
    job = new_job(st2, category="RPO", target=50)
    job.provider = prov
    res, src, cr = job._timed_search(kind, q, 1)
    assert src == "executed" and prov.calls == [], "registry lost on restart"
    print("registry survives restart + applies across categories OK")

    # 6. delete purges the category's registry entries (re-collect possible)
    st2.delete_category("Recruitment")
    assert st2.ledger.executed(ledger_key(kind, q, 1)) is None
    print("delete_category purges registry OK")

    # 7. timeouts: re-sent at most SERPER_TIMEOUT_RETRIES times
    sp = SerperProvider("test-key")
    sp._backoff = staticmethod(lambda *_: 0.0)
    posts = []

    def slow_post(url, json=None, **kw):
        posts.append(json)
        raise requests.Timeout("simulated slow Places answer")
    sp.session.post = slow_post
    try:
        sp.places_page("recruitment agency in Austin", 1)
        raise AssertionError("expected SearchError")
    except SearchError as exc:
        assert exc.kind == "timeout", exc.kind
    assert len(posts) == 1 + config.SERPER_TIMEOUT_RETRIES, len(posts)
    # ... while 5xx (never billed) keeps the full backoff budget
    posts.clear()

    class R:
        status_code, text, headers = 503, "unavailable", {}
    sp.session.post = lambda url, json=None, **kw: (posts.append(json), R())[1]
    try:
        sp.places_page("recruitment agency in Austin", 1)
    except SearchError as exc:
        assert exc.kind == "error"
    assert len(posts) == config.SERPER_MAX_ATTEMPTS, len(posts)
    print(f"timeout re-sends capped at {config.SERPER_TIMEOUT_RETRIES}; "
          f"503 keeps {config.SERPER_MAX_ATTEMPTS} attempts OK")

    print("\nALL CREDIT TESTS PASSED", flush=True)
    from backend.collector import analysis
    if analysis._pool is not None:
        analysis._pool.shutdown(wait=True, cancel_futures=True)
    os._exit(0)


if __name__ == "__main__":
    main()
