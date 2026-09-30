"""Discovery planner: decides what the next Serper credit is spent on.

Measured on the old fixed query list (3D studios / Texas, target 100):
77% of search results were businesses already seen, "top/best X" rephrasings
returned 0-1 new businesses per credit, and city expansion was switched off
whenever a state was pinned - so the run stopped at 56/100 after 24 credits.

How this planner spends credits instead:
  * A search is a (phrase, location) CELL. Phrases = user keywords, category
    templates and category sub-niche phrases; locations = the user's location
    plus its cities (see geo.py).
  * Each cell has a canonical key (content words, business nouns / fillers /
    plurals removed, plus the location), so "3D rendering studios in X",
    "top 3D rendering services in X" and "3D rendering companies in X" are ONE
    cell and only ever paid for once. Used keys persist across runs.
  * The next cell is the one with the highest EXPECTED NOVELTY:
        phrase_quality(p) x location_freshness(g)
    phrase_quality  = smoothed share of the phrase's results that were new
                      businesses (x its valid-record rate once measured)
    location_freshness = 0.9 for an untried location, else the location's
                      recent novelty x decay (another phrase in an already
                      searched place mostly repeats it).
    New markets are therefore explored breadth-first, phrases that return
    repeats are dropped, and the run stops honestly when nothing left is
    expected to produce new businesses.
  * Places page 2..N is requested only for a page that was mostly new
    businesses (the cheapest source of genuinely new ones).

Running out of searches never ends a run by itself. When a tier has nothing
left worth a credit the engine escalates to the next one:
    base      cells scored above MIN_EXPECTED (the measured-yield plan above)
    relaxed   every remaining unused cell, whatever its expected novelty
    expanded  + niche/synonym phrases and nearby locations
    deep      further result pages (Places 2..5, organic 2..3) of every cell
              that produced new businesses
Only when the deep tier is used up too is the search space exhausted - a
finite, logged terminal condition (never an infinite loop).

A failed search is re-queued with exponential backoff (bounded); one that
still fails is recorded and re-tried once on the next run.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

_FILLER = {
    "in", "the", "a", "an", "of", "for", "and", "near", "me", "top", "best",
    "leading", "trusted", "boutique", "independent", "professional",
    "specialist", "local", "rated", "reputable", "affordable", "list", "&",
}
_BIZ_NOUNS = {
    "company", "firm", "agency", "service", "provider", "studio", "business",
    "office", "group", "solution", "specialist", "expert", "shop",
}

UNTRIED_GEO = 0.9         # expected novelty of a location never searched
GEO_DECAY = 0.75          # each extra search in the same place repeats more
MIN_EXPECTED = 0.06       # below this, a search is not worth a credit
FOLLOWUP_MIN_NEW = 0.5    # page N+1 only if page N was >= 50% new
FOLLOWUP_MIN_RESULTS = 8

TIERS = ("base", "relaxed", "expanded", "deep")
TIER_HELP = {
    "relaxed": "trying every remaining phrase x location combination",
    "expanded": "adding niche/synonym phrases and nearby locations",
    "deep": "reading further result pages of productive searches",
}
DEEP_PLACES_MAX_PAGE = 5
DEEP_ORGANIC_MAX_PAGE = 3
RETRY_BASE_S = 5.0        # failed search re-queued after 5 s, 10 s, ...
FAILED_MEMORY_MAX = 500
CELLS_MEMORY_MAX = 20_000


def _stem(w: str) -> str:
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def canon_phrase(phrase: str) -> str:
    toks = [_stem(t) for t in re.findall(r"[a-z0-9&+#]+", phrase.lower())]
    keep = sorted({t for t in toks if t not in _FILLER and t not in _BIZ_NOUNS})
    return " ".join(keep) or " ".join(sorted(set(toks)))


def canon_geo(geo: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", geo.lower()))


@dataclass
class SearchRequest:
    kind: str            # "places" | "organic"
    query: str
    page: int
    phrase: str          # canonical phrase key
    geo: str             # canonical geo key
    expected: float = 0.0
    meta: dict = field(default_factory=dict)


class DiscoveryPlanner:
    def __init__(self, phrases: list[str], geos: list[str], saved: dict | None = None,
                 legacy_done: set[str] | None = None, places: bool = True,
                 max_page: int = 3, extra_phrases: list[str] | None = None,
                 extra_geos: list[str] | None = None):
        self.lock = threading.Lock()
        self.places = places
        self.max_page = max_page
        self.legacy = legacy_done or set()
        # canonical-dedup the phrase list itself (keeps the first spelling)
        self.phrases: list[tuple[str, str]] = []      # (canon, display)
        self._pseen: set[str] = set()
        self.dropped_rephrasings = 0
        for p in phrases:
            self._add_phrase(p)
        self.geos: list[tuple[str, str]] = []
        self._gseen: set[str] = set()
        for g in geos:
            self._add_geo(g)
        # held back until the "expanded" tier
        self.extra_phrases = list(extra_phrases or [])
        self.extra_geos = list(extra_geos or [])
        self.tier = 0
        saved = saved or {}
        self.used: set[str] = set(saved.get("used", []))          # cell / page keys
        # phrase -> [credits, results, new, valid]; geo -> [requests, results, new, rate]
        self.pstat: dict[str, list] = {k: list(v) for k, v in saved.get("phrase", {}).items()}
        self.gstat: dict[str, list] = {k: list(v) for k, v in saved.get("geo", {}).items()}
        # cell key -> {"q", "p", "g", "new", "pp", "pn", "op", "on"}: query text,
        # novelty so far and the last Places / organic page fetched + its size
        # (drives the deep tier; persisted so a resumed run can go deeper).
        self.cells: dict[str, dict] = {k: dict(v) for k, v in saved.get("cells", {}).items()}
        self.inflight_p: dict[str, int] = {}
        self.inflight_g: dict[str, int] = {}
        self.followups: list[SearchRequest] = []
        self.retry_q: list[SearchRequest] = []
        # searches that failed in an earlier run get one fresh attempt now
        for f in saved.get("failed", []):
            try:
                self.retry_q.append(SearchRequest(
                    f["kind"], f["query"], int(f.get("page", 1)), f["phrase"],
                    f["geo"], meta={"attempt": 0, "not_before": 0.0,
                                    "resumed": True}))
            except (KeyError, TypeError, ValueError):
                continue
        self.failed: list[dict] = []
        self.qmap: dict[str, tuple[str, str]] = {}     # query text -> (phrase, geo)
        self.cells_issued = 0
        self.skipped_used = 0
        self.retries_issued = 0

    def _add_phrase(self, p: str) -> bool:
        c = canon_phrase(p)
        if not c or c in self._pseen:
            self.dropped_rephrasings += 1
            return False
        self._pseen.add(c)
        self.phrases.append((c, p))
        return True

    def _add_geo(self, g: str) -> bool:
        c = canon_geo(g)
        if c in self._gseen:
            return False
        self._gseen.add(c)
        self.geos.append((c, g))
        return True

    # -- scoring -------------------------------------------------------------
    def _global_valid_rate(self) -> float:
        cr = sum(v[0] for v in self.pstat.values())
        va = sum(v[3] for v in self.pstat.values())
        return va / cr if cr else 0.0

    def _phrase_q(self, cp: str, idx: int, gvr: float) -> float:
        cr, res, new, valid = self.pstat.get(cp, [0, 0, 0, 0])
        q = (new + 2.1) / (res + 3.0)          # untried phrase -> 0.70
        if cr >= 3 and gvr > 0:
            q *= min(1.0, max(0.3, (valid / cr) / gvr))
        if cr == 0:
            q -= 0.002 * idx                    # keep the user's order among untried
        return q * (GEO_DECAY ** self.inflight_p.get(cp, 0) if cr == 0 else 1.0)

    def _geo_f(self, cg: str) -> float:
        n, _res, _new, rate = self.gstat.get(cg, [0, 0, 0, 0.0])
        f = UNTRIED_GEO if n == 0 else rate * GEO_DECAY
        return f * (GEO_DECAY ** self.inflight_g.get(cg, 0))

    def _cell_key(self, cp: str, cg: str) -> str:
        return f"{cp}@{cg}"

    @staticmethod
    def query_text(phrase: str, geo: str) -> str:
        return f"{phrase} in {geo}" if geo else phrase

    # -- API -----------------------------------------------------------------
    def next_requests(self, want_organic: bool) -> list[SearchRequest]:
        """The requests for the next search: a due retry, the most promising
        unused cell (Places and/or organic), a Places follow-up page, or - in
        the deep tier - a further page of a productive cell. [] = nothing left
        in the current tier (the engine then escalates) or only retries that
        are not due yet."""
        with self.lock:
            now = time.time()
            for i, r in enumerate(self.retry_q):
                if r.meta.get("not_before", 0.0) <= now:
                    self.retry_q.pop(i)
                    self.retries_issued += 1
                    self._inflight(r, +1)
                    return [r]
            gvr = self._global_valid_rate()
            # base tier: only cells worth a credit; later tiers: any unused cell
            best, best_e = None, (MIN_EXPECTED if self.tier == 0 else -1.0)
            for i, (cp, disp_p) in enumerate(self.phrases):
                q = self._phrase_q(cp, i, gvr)
                if q * UNTRIED_GEO <= best_e:
                    continue
                for cg, disp_g in self.geos:
                    key = self._cell_key(cp, cg)
                    if key in self.used:
                        continue
                    e = q * self._geo_f(cg)
                    if e > best_e:
                        text = self.query_text(disp_p, disp_g)
                        if text.lower() in self.legacy:   # executed by an older version
                            self.used.add(key)
                            self.skipped_used += 1
                            continue
                        best, best_e = (cp, cg, text), e
            self.followups.sort(key=lambda r: -r.expected)
            if self.followups and (self.followups[0].expected >= best_e
                                   or best is None):
                req = self.followups.pop(0)
                self._inflight(req, +1)
                return [req]
            if best is None:
                return self._deep_request(want_organic) if self.tier >= 3 else []
            cp, cg, text = best
            self.used.add(self._cell_key(cp, cg))
            self.qmap[text.lower()] = (cp, cg)
            self.cells_issued += 1
            reqs = []
            if self.places:
                reqs.append(SearchRequest("places", text, 1, cp, cg, best_e))
            if want_organic or not self.places:
                reqs.append(SearchRequest("organic", text, 1, cp, cg, best_e))
            for r in reqs:
                self._inflight(r, +1)
            return reqs

    def _inflight(self, req: SearchRequest, d: int):
        self.inflight_p[req.phrase] = max(0, self.inflight_p.get(req.phrase, 0) + d)
        self.inflight_g[req.geo] = max(0, self.inflight_g.get(req.geo, 0) + d)

    def on_result(self, req: SearchRequest, results: int, new: int, credits: int = 1):
        with self.lock:
            self._inflight(req, -1)
            ck = self._cell_key(req.phrase, req.geo)
            cell = self.cells.get(ck)
            if cell is None and len(self.cells) < CELLS_MEMORY_MAX:
                cell = self.cells[ck] = {"q": req.query, "p": req.phrase, "g": req.geo,
                                         "new": 0, "pp": 0, "pn": 0, "op": 0, "on": 0}
            if cell is not None:
                cell["new"] += new
                pk, nk = ("pp", "pn") if req.kind == "places" else ("op", "on")
                if req.page >= cell[pk]:
                    cell[pk], cell[nk] = req.page, results
            ps = self.pstat.setdefault(req.phrase, [0, 0, 0, 0])
            ps[0] += credits
            ps[1] += results
            ps[2] += new
            gs = self.gstat.setdefault(req.geo, [0, 0, 0, 0.0])
            rate = (new / results) if results else 0.0
            gs[3] = rate if gs[0] == 0 else 0.5 * gs[3] + 0.5 * rate
            gs[0] += 1
            gs[1] += results
            gs[2] += new
            if (req.kind == "places" and self.places and req.page < self.max_page
                    and results >= FOLLOWUP_MIN_RESULTS
                    and new / results >= FOLLOWUP_MIN_NEW):
                key = f"{self._cell_key(req.phrase, req.geo)}#p{req.page + 1}"
                if key not in self.used:
                    self.used.add(key)
                    self.followups.append(SearchRequest(
                        "places", req.query, req.page + 1, req.phrase, req.geo,
                        expected=rate * 0.95))

    def on_skipped(self, req: SearchRequest):
        """The request was never sent (job finishing): just release it."""
        with self.lock:
            self._inflight(req, -1)

    def on_failed(self, req: SearchRequest, error: str = "", retries: int = 2) -> str:
        """A search failed after the provider's own retries. Re-queue it with
        exponential backoff (at most `retries` times); after that record it as
        failed - the run carries on either way. Returns "retry" or "failed"."""
        with self.lock:
            self._inflight(req, -1)
            attempt = int(req.meta.get("attempt", 0))
            if attempt < retries:
                req.meta["attempt"] = attempt + 1
                req.meta["not_before"] = time.time() + RETRY_BASE_S * (2 ** attempt)
                req.meta["error"] = error[:160]
                self.retry_q.append(req)
                return "retry"
            if len(self.failed) < FAILED_MEMORY_MAX:
                self.failed.append({
                    "kind": req.kind, "query": req.query, "page": req.page,
                    "phrase": req.phrase, "geo": req.geo, "error": error[:160],
                    "attempts": attempt + 1,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            return "failed"

    def requeue(self, req: SearchRequest):
        """Put a request that was never answered (provider paused) back in
        the queue unchanged - it is not a failed attempt."""
        with self.lock:
            self._inflight(req, -1)
            req.meta["not_before"] = 0.0
            self.retry_q.append(req)

    def pending_retries(self) -> int:
        with self.lock:
            return len(self.retry_q)

    def expedite_retries(self) -> int:
        """Watchdog: make every queued retry due now."""
        with self.lock:
            for r in self.retry_q:
                r.meta["not_before"] = 0.0
            return len(self.retry_q)

    def inflight(self) -> int:
        with self.lock:
            return sum(self.inflight_p.values())

    @property
    def tier_name(self) -> str:
        return TIERS[self.tier]

    def escalate(self) -> str | None:
        """Move to the next search-strategy tier. Returns its name, or None
        when the last tier is already active (search space exhausted)."""
        with self.lock:
            if self.tier >= len(TIERS) - 1:
                return None
            self.tier += 1
            if TIERS[self.tier] == "expanded":
                for p in self.extra_phrases:
                    self._add_phrase(p)
                for g in self.extra_geos:
                    self._add_geo(g)
            return TIERS[self.tier]

    def _deep_request(self, want_organic: bool) -> list[SearchRequest]:
        """Deep tier (lock held): the next result page of the cell with the
        most new businesses whose previous page was full (a short page means
        the list ended)."""
        best, best_new = None, 0
        for ck, c in self.cells.items():
            if c.get("new", 0) <= best_new:
                continue
            nxt = None
            if (self.places and 1 <= c["pp"] < DEEP_PLACES_MAX_PAGE
                    and c["pn"] >= FOLLOWUP_MIN_RESULTS
                    and f"{ck}#p{c['pp'] + 1}" not in self.used):
                nxt = ("places", c["pp"] + 1, f"{ck}#p{c['pp'] + 1}")
            elif (want_organic or not self.places) and 1 <= c["op"] < DEEP_ORGANIC_MAX_PAGE \
                    and c["on"] >= 8 and f"{ck}#o{c['op'] + 1}" not in self.used:
                nxt = ("organic", c["op"] + 1, f"{ck}#o{c['op'] + 1}")
            if nxt:
                best, best_new = (c, nxt), c["new"]
        if best is None:
            return []
        c, (kind, page, key) = best
        self.used.add(key)
        req = SearchRequest(kind, c["q"], page, c["p"], c["g"],
                            expected=0.0, meta={"deep": True})
        self._inflight(req, +1)
        return [req]

    def on_valid(self, query: str):
        with self.lock:
            pg = self.qmap.get((query or "").lower())
            if pg:
                self.pstat.setdefault(pg[0], [0, 0, 0, 0])[3] += 1

    def candidate_cells(self) -> int:
        with self.lock:
            return sum(1 for cp, _ in self.phrases for cg, _ in self.geos
                       if self._cell_key(cp, cg) not in self.used)

    def summary(self) -> dict:
        with self.lock:
            top = sorted(self.pstat.items(), key=lambda kv: -kv[1][2])[:5]
            return {
                "phrases": len(self.phrases), "locations": len(self.geos),
                "cells_issued": self.cells_issued,
                "rephrasings_dropped": self.dropped_rephrasings,
                "followups_queued": len(self.followups),
                "tier": TIERS[self.tier],
                "retries_pending": len(self.retry_q),
                "retries_issued": self.retries_issued,
                "failed_searches": len(self.failed),
                "top_phrases": [{"phrase": k, "credits": v[0], "results": v[1],
                                 "new": v[2], "valid": v[3]} for k, v in top],
            }

    def export(self) -> dict:
        with self.lock:
            # failed + still-queued retries are handed to the next run
            pending = [{"kind": r.kind, "query": r.query, "page": r.page,
                        "phrase": r.phrase, "geo": r.geo,
                        "error": r.meta.get("error", "")} for r in self.retry_q]
            return {"used": sorted(self.used),
                    "phrase": {k: list(v) for k, v in self.pstat.items()},
                    "geo": {k: list(v) for k, v in self.gstat.items()},
                    "cells": {k: dict(v) for k, v in self.cells.items()},
                    "failed": (self.failed + pending)[:FAILED_MEMORY_MAX],
                    "generated_phrases": [d for _, d in self.phrases],
                    "generated_locations": [d for _, d in self.geos],
                    "tier": TIERS[self.tier]}
