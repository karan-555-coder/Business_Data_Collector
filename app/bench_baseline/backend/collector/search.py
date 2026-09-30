"""Search provider abstraction. Serper.dev (Google organic + Places) is the
only implemented provider; add another by subclassing SearchProvider and
registering it in PROVIDERS. Ported from business_collector.py's Serper client
(429 backoff, 401/403/credit detection, credit accounting)."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from abc import ABC, abstractmethod

import requests

from .. import config

log = logging.getLogger("search")

# Transient: retried with exponential backoff. Anything else in 4xx is
# permanent for that request (never retried); 401/403/credit = provider dead.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_CREDIT_RE = re.compile(r"credit|quota|balance|not enough", re.I)
BACKOFF_CAP_S = 30.0


class AdaptiveRate:
    """Adaptive request pacing: paces calls at `rps`, halves the rate on 429
    or repeated timeouts, and creeps back up after sustained successes.
    Never exceeds the configured ceiling."""

    def __init__(self, max_rps: float):
        self.max_rps = max(1.0, max_rps)
        self.rps = self.max_rps
        self._next = 0.0
        self._ok_streak = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + 1.0 / self.rps
        if wait > 0:
            time.sleep(wait)

    def on_ok(self):
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= 25 and self.rps < self.max_rps:
                self.rps = min(self.max_rps, self.rps + 1)
                self._ok_streak = 0

    def on_backoff(self):
        with self._lock:
            self.rps = max(1.0, self.rps / 2)
            self._ok_streak = 0
            log.warning("adaptive rate reduced to %.1f req/s", self.rps)


class SearchError(Exception):
    """Transient search failure (throttled, 5xx, network)."""


class ProviderDisabled(Exception):
    """Key invalid / credits exhausted: no further requests until resolved.
    kind="credits": the account is out of credits - the job pauses and
    resumes by itself after a top-up. kind="auth": the key is rejected -
    genuinely unrecoverable without user action."""

    def __init__(self, message: str, kind: str = "auth"):
        super().__init__(message)
        self.kind = kind


class SearchProvider(ABC):
    name = "base"
    supports_places = False

    @abstractmethod
    def organic(self, query: str, max_results: int) -> list[str]:
        """Return organic result URLs."""

    def places(self, query: str, pages: int) -> list[dict]:
        return []

    def places_page(self, query: str, page: int) -> list[dict]:
        """One page of Places results (page 1 by default via places())."""
        return self.places(query, 1) if page == 1 else []

    def organic_page(self, query: str, page: int) -> list[str]:
        """One page of organic results (deep-search tier)."""
        return self.organic(query, 10) if page == 1 else []

    def balance(self) -> int | None:
        """Remaining search credits, or None when unknown."""
        return None

    def reenable(self):
        """Clear a credits-exhausted state after a top-up."""

    @property
    def requests_made(self) -> int:
        return 0


class SerperProvider(SearchProvider):
    name = "serper"
    supports_places = True

    SEARCH_URL = "https://google.serper.dev/search"
    PLACES_URL = "https://google.serper.dev/places"

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderDisabled(
                "SERPER_API_KEY is not set. Put it in app/.env (see .env.example).")
        self.session = requests.Session()
        # keep-alive pool sized to the search concurrency (requests' default
        # of 10 would silently serialise anything above that)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2, pool_maxsize=config.SERPER_CONCURRENCY + 4)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        })
        self.credits_used = 0
        self._requests = 0
        self.ok_count = 0
        self.fail_count = 0
        self.retry_count = 0
        self.disabled_kind = ""
        self.disabled_reason = ""
        self.lock = threading.Lock()
        self.rate = AdaptiveRate(config.SERPER_RPS)
        self.on_event = None   # optional callable("429"|"timeout"|"retry")

    def _event(self, kind: str):
        if self.on_event is not None:
            try:
                self.on_event(kind)
            except Exception:
                pass

    @property
    def requests_made(self) -> int:
        return self._requests

    def _log_req(self, url: str, payload: dict, t_start: float, status, n: int):
        t_end = time.time()
        kind = "places" if url == self.PLACES_URL else "search"
        log.info("serper %-6s q=%r page=%s start=%s end=%s dur=%dms status=%s results=%d",
                 kind, payload.get("q", ""), payload.get("page", 1),
                 time.strftime("%H:%M:%S", time.localtime(t_start)),
                 time.strftime("%H:%M:%S", time.localtime(t_end)),
                 (t_end - t_start) * 1000, status, n)

    def _count(self, attr: str, n: int = 1):
        with self.lock:
            setattr(self, attr, getattr(self, attr) + n)

    @staticmethod
    def _backoff(attempt: int, retry_after: float) -> float:
        """Exponential backoff with jitter: ~1 s, 2 s, 4 s ... capped; a
        server Retry-After is honoured up to the cap (never hours-long)."""
        base = min(BACKOFF_CAP_S, 2.0 ** (attempt - 1))
        delay = base + random.uniform(0, 0.5 * base)
        return min(BACKOFF_CAP_S, max(delay, retry_after))

    def _post(self, url: str, payload: dict) -> dict:
        """One logical Serper request. Tight timeouts (a 35 s response must
        never stall the pipeline), bounded retries of TRANSIENT failures only
        (timeout, connection/DNS error, malformed body, 408/425/429/5xx) with
        exponential backoff + adaptive pacing. Permanent 4xx fail this request
        only; 401/403/credit errors disable the provider. Every HTTP attempt
        is logged with query, timing, status and result count."""
        if self.disabled_reason:
            raise ProviderDisabled(self.disabled_reason, self.disabled_kind)
        last, retry_after = "", 0.0
        attempts = config.SERPER_MAX_ATTEMPTS
        for attempt in range(attempts):
            if attempt:
                delay = self._backoff(attempt, retry_after)
                self._count("retry_count")
                self._event("retry")
                log.warning("serper retry %d/%d in %.1fs after %s (q=%r)",
                            attempt, attempts - 1, delay, last, payload.get("q", ""))
                time.sleep(delay)
                if self.disabled_reason:   # another thread hit a fatal error
                    raise ProviderDisabled(self.disabled_reason, self.disabled_kind)
            retry_after = 0.0
            self.rate.acquire()
            t_start = time.time()
            try:
                resp = self.session.post(url, json=payload,
                                         timeout=config.SERPER_TIMEOUT)
            except requests.Timeout:
                self._count("_requests")
                self._log_req(url, payload, t_start, "TIMEOUT", 0)
                self._event("timeout")
                self.rate.on_backoff()
                last = "timeout"
                continue
            except requests.ConnectionError as exc:   # DNS / refused / reset
                self._log_req(url, payload, t_start, "NETERR", 0)
                last = f"network error: {str(exc)[:100]}"
                continue
            except requests.RequestException as exc:  # malformed request: permanent
                self._log_req(url, payload, t_start, "REQERR", 0)
                self._count("fail_count")
                raise SearchError(f"serper request error: {str(exc)[:120]}") from exc
            self._count("_requests")
            status = resp.status_code
            if status == 200:
                try:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError("not a JSON object")
                except ValueError:
                    self._log_req(url, payload, t_start, "BADJSON", 0)
                    last = "malformed response body"
                    continue
                self.rate.on_ok()
                n = len(data.get("places") or data.get("organic") or [])
                self._log_req(url, payload, t_start, 200, n)
                with self.lock:
                    self.credits_used += int(data.get("credits", 1) or 1)
                    self.ok_count += 1
                return data
            self._log_req(url, payload, t_start, status, 0)
            body = resp.text[:200]
            if status in (401, 402, 403) or (400 <= status < 500 and status != 429
                                             and _CREDIT_RE.search(body)):
                self._count("fail_count")
                self.disabled_kind = ("credits" if status == 402 or _CREDIT_RE.search(body)
                                      else "auth")
                self.disabled_reason = f"Serper HTTP {status}: {body}"
                raise ProviderDisabled(self.disabled_reason, self.disabled_kind)
            if status in RETRYABLE_STATUS:
                if status == 429:
                    self._event("429")
                    self.rate.on_backoff()
                try:
                    retry_after = float(resp.headers.get("Retry-After") or 0)
                except ValueError:
                    retry_after = 0.0
                last = f"HTTP {status}"
                continue
            self._count("fail_count")   # other 4xx: permanent, never retried
            raise SearchError(f"serper HTTP {status}: {body}")
        self._count("fail_count")
        raise SearchError(f"serper: gave up after {attempts} attempts ({last})")

    def organic(self, query: str, max_results: int) -> list[str]:
        """Exactly ONE credit: a single page of up to 10 results. (The old
        loop silently paid for page 2 and 3 whenever Google returned fewer
        than 10 organic links, e.g. when a local pack took space.)"""
        data = self._post(self.SEARCH_URL, {"q": query, "num": 10})
        urls: list[str] = []
        for item in data.get("organic") or []:
            link = item.get("link") or ""
            if link.startswith("http") and link not in urls:
                urls.append(link)
        return urls[:max_results]

    ACCOUNT_URL = "https://google.serper.dev/account"

    def balance(self) -> int | None:
        """Remaining credits from Serper's account endpoint (costs no credit).
        None if it can't be read right now."""
        try:
            r = self.session.get(self.ACCOUNT_URL, timeout=config.SERPER_TIMEOUT)
            if r.status_code == 200:
                return int(r.json().get("balance"))
            log.warning("serper balance check: HTTP %s", r.status_code)
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("serper balance check failed: %s", str(exc)[:120])
        return None

    def reenable(self):
        if self.disabled_kind == "credits":
            self.disabled_reason, self.disabled_kind = "", ""

    def organic_page(self, query: str, page: int) -> list[str]:
        """Organic results page N (1 credit). Used by the deep-search tier."""
        if page <= 1:
            return self.organic(query, 10)
        data = self._post(self.SEARCH_URL, {"q": query, "num": 10, "page": page})
        urls: list[str] = []
        for item in data.get("organic") or []:
            link = item.get("link") or ""
            if link.startswith("http") and link not in urls:
                urls.append(link)
        return urls

    def places_page(self, query: str, page: int) -> list[dict]:
        data = self._post(self.PLACES_URL, {"q": query, "page": page})
        out: list[dict] = []
        seen: set[str] = set()
        for p in data.get("places") or []:
            key = str(p.get("cid") or p.get("placeId") or p.get("title"))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def places(self, query: str, pages: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, pages + 1):
            rows = self.places_page(query, page)
            out.extend(rows)
            if len(rows) < 10:
                break
        return out


def make_provider(name: str, serper_key: str) -> SearchProvider:
    if name.lower() == "serper":
        return SerperProvider(serper_key)
    raise ProviderDisabled(f"Unknown search provider: {name}")
