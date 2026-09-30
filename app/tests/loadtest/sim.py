"""Simulated Serper + simulated business websites for load tests.

The REAL SerperProvider code runs (rate limiter, retries, 429 backoff, credit
accounting); only its HTTP session is replaced by FakeSerperSession, which
enforces a simulated account-wide rate limit (Serper answers 429 above it)
and realistic latency. Websites are served by SimFetcher: realistic page
sizes (so HTML parsing costs real CPU in the analysis processes), latency,
dead hosts and slow hosts. No network, no credits.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time

FIRST = ["Adler", "Brooks", "Carver", "Dalton", "Ellis", "Fisher", "Garner",
         "Hayes", "Irving", "Jensen", "Keller", "Lawson", "Mercer", "Nolan",
         "Ortiz", "Parker", "Quinn", "Ramsey", "Sutton", "Tanner", "Upton",
         "Vaughn", "Walker", "Xavier", "Yates", "Zimmer", "Harlow", "Kendall",
         "Monroe", "Prescott"]
SECOND = ["Advisory", "Partners", "Group", "Associates", "Consulting", "Capital",
          "Legal", "Health", "Talent", "Works", "Labs", "Studio", "Solutions",
          "Services", "Networks", "Systems", "Holdings", "Ventures", "Bridge",
          "Summit", "Harbor", "Pioneer", "Keystone", "Beacon", "Compass",
          "Anchor", "Vertex", "Liberty", "Frontier", "Heritage"]
CITIES = [("Austin", "Texas", "78701"), ("Dallas", "Texas", "75201"),
          ("Chicago", "Illinois", "60601"), ("Denver", "Colorado", "80202"),
          ("Seattle", "Washington", "98101"), ("Atlanta", "Georgia", "30303"),
          ("Boston", "Massachusetts", "02108"), ("Miami", "Florida", "33101")]
WORLD_SIZE = 900_000
TIME_SCALE = 1.0      # multiply every simulated latency (tests use ~0.05)


def h(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def lognormal(median: float, sigma: float, cap: float) -> float:
    return TIME_SCALE * min(cap, median * math.exp(random.gauss(0, sigma)))


def pause(lo: float, hi: float):
    time.sleep(TIME_SCALE * random.uniform(lo, hi))


def biz(i: int) -> dict:
    i %= WORLD_SIZE
    name = f"{FIRST[i % 30]} {SECOND[(i // 30) % 30]} {i}"
    dom = f"{FIRST[i % 30].lower()}{SECOND[(i // 30) % 30].lower()}{i}.example"
    city, state, z = CITIES[i % len(CITIES)]
    return {"id": i, "name": name, "domain": dom,
            "has_site": h("site", i) % 10 < 7,
            "phone": f"+1 {2000000000 + i}",
            "email": f"info@{dom}",
            "address": f"{100 + i % 800} Main Street, {city}, {state} {z}, USA"}


def ids_for(query: str, page: int, n: int = 10) -> list[int]:
    """Results depend on the whole query (phrase + place): overlapping
    windows give realistic duplicates, new queries surface new businesses."""
    base = h("q", " ".join(query.lower().split())) % WORLD_SIZE
    rng = random.Random(h("p", query.lower(), page))
    return [(base + k) % WORLD_SIZE for k in rng.sample(range(400), n)]


# --------------------------------------------------------------------------- #
# Serper stand-in (HTTP level)
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers: dict = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class SerperAccount:
    """Process-wide simulated Serper account: rate limit + credit counter."""

    def __init__(self, rps: float = 5.0, balance: int = 10**9):
        self.rps = rps
        self.balance = balance
        self.lock = threading.Lock()
        self.window: list[float] = []
        self.calls = 0
        self.throttled = 0
        self.inflight = 0
        self.max_inflight = 0

    def admit(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.window = [t for t in self.window if now - t < 1.0]
            if len(self.window) >= self.rps:
                self.throttled += 1
                return False
            self.window.append(now)
            self.calls += 1
            self.balance -= 1
            return True


ACCOUNT = SerperAccount()


class FakeSerperSession:
    """Drop-in for SerperProvider.session (post / get / headers)."""

    def __init__(self, account: SerperAccount = ACCOUNT):
        self.account = account
        self.headers: dict = {}

    def mount(self, *a, **kw):
        pass

    def get(self, url, timeout=None):
        return _Resp(200, {"balance": self.account.balance,
                           "rateLimit": self.account.rps})

    def post(self, url, json=None, timeout=None):
        q = (json or {}).get("q", "")
        page = int((json or {}).get("page", 1) or 1)
        if not self.account.admit():
            pause(0.05, 0.05)
            return _Resp(429, None, "Too many requests")
        with self.account.lock:
            self.account.inflight += 1
            self.account.max_inflight = max(self.account.max_inflight,
                                            self.account.inflight)
        try:
            time.sleep(lognormal(1.2, 0.45, 9.0))
        finally:
            with self.account.lock:
                self.account.inflight -= 1
        ids = ids_for(q, page)
        if url.endswith("/places"):
            rows = []
            for i in ids:
                b = biz(i)
                row = {"title": b["name"], "phoneNumber": b["phone"],
                       "cid": str(7_000_000 + i), "address": b["address"],
                       "category": "Business service", "rating": 4.5,
                       "ratingCount": 12, "thumbnailUrl": "https://x/y.png"}
                if b["has_site"]:
                    row["website"] = f"https://{b['domain']}"
                rows.append(row)
            return _Resp(200, {"places": rows, "credits": 1})
        organic = [{"link": f"https://{biz(i)['domain']}/", "title": biz(i)["name"]}
                   for i in ids if biz(i)["has_site"]]
        organic.append({"link": "https://www.linkedin.com/company/some-firm"})
        return _Resp(200, {"organic": organic, "credits": 1})


def make_sim_provider_factory(real_make_provider):
    """engine.make_provider replacement: the real SerperProvider, fake wire."""
    def make(name, key):
        prov = real_make_provider(name, key or "sim-key")
        prov.session = FakeSerperSession()
        return prov
    return make


def install():
    """Patch the engine in THIS process: simulated Serper wire + websites."""
    from backend.collector import engine
    if getattr(engine, "_sim_installed", False):
        return
    engine.make_provider = make_sim_provider_factory(engine.make_provider)
    engine.Fetcher = SimFetcher
    engine._sim_installed = True


# --------------------------------------------------------------------------- #
# Website stand-in
# --------------------------------------------------------------------------- #

_FILLER = " ".join(
    f"<p>Our team delivers {w} services with a focus on quality, compliance "
    f"and measurable outcomes for clients across many industries.</p>"
    for w in ("advisory", "consulting", "audit", "tax", "strategy", "people",
              "operations", "technology", "risk", "growth") * 12)
_NAV = "".join(f'<li><a href="/section-{k}">Section {k}</a></li>' for k in range(60))
_FOOT = "".join(f'<a href="https://partner{k}.example/">Partner {k}</a> '
                for k in range(25))


class SimFetcher:
    """Replaces crawler.Fetcher inside the engine (fetch_raw only)."""

    def __init__(self):
        self.timings = None

    @staticmethod
    def _id(url: str) -> int:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        digits = "".join(ch for ch in host.split(".")[0] if ch.isdigit())
        return int(digits) if digits else -1

    def fetch_raw(self, url: str):
        i = self._id(url)
        roll = random.random()
        if i < 0 or roll < 0.08:                  # dead host / DNS failure
            pause(0.02, 0.3)
            return None, url, "connection/DNS error"
        if roll < 0.10:                           # slow host
            pause(4, 8)
        else:
            time.sleep(lognormal(0.5, 0.6, 6.0))
        b = biz(i)
        contact_page = "/contact" in url
        rich = h("rich", i) % 10 < 6              # homepage has everything
        contact = (f'<a href="mailto:{b["email"]}">Email</a> '
                   f'<a href="tel:{b["phone"]}">Call {b["phone"]}</a>'
                   f'<address>{b["address"]}</address>') if (rich or contact_page) else \
            f'<a href="tel:{b["phone"]}">Call {b["phone"]}</a>'
        html = (f"<html><head><title>{b['name']} | Professional Services</title>"
                f'<meta name="description" content="{b["name"]} professional services">'
                f"</head><body><nav><ul>{_NAV}</ul></nav><h1>{b['name']}</h1>"
                f"{contact}<main>{_FILLER}</main>"
                f'<a href="/contact">Contact us</a> <a href="/about">About</a>'
                f"<footer>{_FOOT}</footer></body></html>")
        final = url if url.startswith("https://") else f"https://{b['domain']}/"
        return html.encode(), final, ""
