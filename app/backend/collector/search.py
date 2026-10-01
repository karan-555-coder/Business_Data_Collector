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
from .tls import SharedTLSAdapter

log = logging.getLogger("search")

# Transient: retried with exponential backoff. Anything else in 4xx is
# permanent for that request (never retried); 401/403/credit = provider dead.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_CREDIT_RE = re.compile(r"credit|quota|balance|not enough", re.I)
BACKOFF_CAP_S = 30.0


class AdaptiveRate:
    """Adaptive request pacing: paces calls at `rps`, backs off on 429 or
    timeouts, and creeps back up after sustained successes. Never exceeds
    the ceiling - the configured SERPER_RPS, lowered to the account's own
    advertised rate limit once known (set_ceiling).

    Measured with 4 concurrent jobs on a 5 req/s account: halving on every
    429 from an 8 req/s ceiling, then +1 per 25 successes, sawtoothed at
    ~2.4 req/s - half the account's capacity. A 429 is never billed, so a
    milder step (x0.7) and a ceiling at the real limit keep it near 5."""

    DECREASE = 0.7
    INCREASE_EVERY = 10          # successes per +0.5 req/s

    def __init__(self, max_rps: float):
        self.max_rps = max(1.0, max_rps)
        self.rps = self.max_rps
        self._next = 0.0
        self._ok_streak = 0
        self._lock = threading.Lock()

    def set_ceiling(self, rps: float):
        with self._lock:
            new = max(1.0, min(float(config.SERPER_RPS), float(rps)))
            if abs(new - self.max_rps) >= 0.01:
                log.info("Serper pacing ceiling: %.1f req/s (account limit %s)",
                         new, rps)
            self.max_rps = new
            self.rps = min(self.rps, new)

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
            if self._ok_streak >= self.INCREASE_EVERY and self.rps < self.max_rps:
                self.rps = min(self.max_rps, self.rps + 0.5)
                self._ok_streak = 0

    def on_backoff(self):
        with self._lock:
            self.rps = max(1.0, self.rps * self.DECREASE)
            self._ok_streak = 0
            log.warning("adaptive rate reduced to %.1f req/s", self.rps)


class SearchError(Exception):
    """Transient search failure (throttled, 5xx, network). kind="timeout"
    when the last attempt timed out: Serper may already have processed (and
    charged) it, so callers re-queue such a search at most once."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


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

    def __init__(self, api_key: str, account: "AccountLimits | None" = None):
        if not api_key:
            raise ProviderDisabled(
                "SERPER_API_KEY is not set. Put it in app/.env (see .env.example).")
        # Shared account limits (engine jobs) or a private set (standalone use)
        if account is not None:
            self.session = account.session
            self.rate = account.rate
            self._slots = account.slots
        else:
            self.session = _serper_session()
            self.rate = AdaptiveRate(config.SERPER_RPS)
            self._slots = None
        self.session.headers["X-API-KEY"] = api_key
        self.credits_used = 0
        self._requests = 0
        self.ok_count = 0
        self.fail_count = 0
        self.retry_count = 0
        self.disabled_kind = ""
        self.disabled_reason = ""
        self.lock = threading.Lock()
        self.on_event = None   # optional callable("429"|"timeout"|"retry")
        # timeouts of the calling thread's last request (credit ledger: a
        # timed-out attempt may have been charged server-side)
        self._tl = threading.local()

    @property
    def last_timeouts(self) -> int:
        return getattr(self._tl, "timeouts", 0)

    @property
    def last_credits(self) -> int:
        """Credits Serper billed for the calling thread's last request."""
        return getattr(self._tl, "credits", 1)

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
        # time on the wire, not time spent waiting for a slot / pacing
        t_start = max(t_start, getattr(self._tl, "t_sent", 0.0))
        kind = "places" if url == self.PLACES_URL else "search"
        log.info("serper %-6s q=%r page=%s start=%s end=%s dur=%dms status=%s results=%d",
                 kind, payload.get("q", ""), payload.get("page", 1),
                 time.strftime("%H:%M:%S", time.localtime(t_start)),
                 time.strftime("%H:%M:%S", time.localtime(t_end)),
                 (t_end - t_start) * 1000, status, n)

    def _send(self, url: str, payload: dict):
        """One HTTP attempt inside the account limits: an in-flight slot
        (shared by all jobs), then pacing, then the request."""
        slots = self._slots
        if slots is not None:
            slots.acquire()
        try:
            self.rate.acquire()
            self._tl.t_sent = time.time()
            return self.session.post(url, json=payload, timeout=config.SERPER_TIMEOUT)
        finally:
            if slots is not None:
                slots.release()

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
        self._tl.timeouts = 0
        for attempt in range(attempts):
            # Credit guard: 429 / 5xx / connection errors are never charged,
            # so they get the full backoff budget - but a request that timed
            # out may have completed (and been billed) server-side. Resending
            # it over and over is how one slow query was paid for 3-4 times.
            if self._tl.timeouts > config.SERPER_TIMEOUT_RETRIES:
                break
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
            t_start = time.time()
            try:
                resp = self._send(url, payload)
            except requests.Timeout:
                self._count("_requests")
                self._log_req(url, payload, t_start, "TIMEOUT", 0)
                self._event("timeout")
                self.rate.on_backoff()
                self._tl.timeouts += 1
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
                self._tl.credits = int(data.get("credits", 1) or 1)
                with self.lock:
                    self.credits_used += self._tl.credits
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
        raise SearchError(f"serper: gave up ({last}; {self._tl.timeouts} timeout(s))",
                          kind="timeout" if last == "timeout" else "error")

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
        None if it can't be read right now. Picks up a key changed in
        app/.env since the job started (no restart needed)."""
        key = config.serper_api_key()
        if key and key != self.session.headers.get("X-API-KEY"):
            self.session.headers["X-API-KEY"] = key
            log.info("serper: API key changed in app/.env - using the new key")
        try:
            r = self.session.get(self.ACCOUNT_URL, timeout=config.SERPER_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                try:   # the account's own requests/second limit
                    limit = float(data.get("rateLimit") or 0)
                    if limit > 0:
                        self.rate.set_ceiling(limit)
                except (TypeError, ValueError):
                    pass
                return int(data.get("balance"))
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


class AccountLimits:
    """Serper limits are per ACCOUNT, not per job: every job in the process
    shares one adaptive rate limiter (a 429 slows everyone down, as it
    must) and one semaphore capping requests in flight at
    SERPER_CONCURRENCY. Waiters are served roughly first-come-first-served,
    so concurrent jobs get fair turns instead of racing into 429 storms."""

    def __init__(self):
        self.rate = AdaptiveRate(config.SERPER_RPS)
        self.slots = threading.BoundedSemaphore(config.SERPER_CONCURRENCY)
        self.session = _serper_session()


_account: AccountLimits | None = None
_account_lock = threading.Lock()


def account_limits() -> AccountLimits:
    global _account
    with _account_lock:
        if _account is None:
            _account = AccountLimits()
        return _account


def _serper_session() -> requests.Session:
    s = requests.Session()
    # keep-alive pool sized to the search concurrency (requests' default
    # of 10 would silently serialise anything above that)
    adapter = SharedTLSAdapter(
        pool_connections=2, pool_maxsize=config.SERPER_CONCURRENCY + 4)
    s.mount("https://", adapter)
    s.headers.update({"Content-Type": "application/json"})
    return s


def make_provider(name: str, serper_key: str) -> SearchProvider:
    """A provider for one job, bound to the process-wide account limits."""
    if name.lower() == "serper":
        return SerperProvider(serper_key, account=account_limits())
    raise ProviderDisabled(f"Unknown search provider: {name}")
