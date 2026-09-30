"""Website crawling: HTTP session, robots.txt cache, page fetching, contact
page discovery, directory link mining. Ported from business_collector.py,
including the Retry-After fix (hostile sites send hours-long values)."""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import config
from ..config import FETCH_TIMEOUT
from .normalize import root_domain
from .ssrf import url_block_reason

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CONTACT_LINK_RE = re.compile(
    r"contact|about|reach-?us|get-?in-?touch|locations?|offices?|find-?us|"
    r"visit-?us", re.I
)
MAX_CANDIDATES_PER_PAGE = 60
MAX_REDIRECTS = 5
crawl_log = logging.getLogger("crawler")

# lxml parses large pages notably faster than the pure-Python parser;
# fall back transparently when it isn't installed.
import importlib.util
BS_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"


def make_session(retries: int = 1) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        # connect=0: a connection failure is almost always a dead host -
        # retrying doubles the cost of every dead site for near-zero gain.
        # Transient read/5xx failures still get one retry with backoff.
        total=retries, connect=0, read=retries, backoff_factor=0.3,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        # Never honor server-sent Retry-After: broken sites send hours-long
        # values and freeze worker threads.
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry,
                          pool_connections=config.KEEPALIVE_CONNECTIONS,
                          pool_maxsize=config.MAX_CONNECTIONS)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s


class RobotsCache:
    """Per-host robots.txt cache. Missing/unreachable robots.txt => allowed,
    except when the host itself is dead (skip to save time)."""

    DEAD = "dead"

    def __init__(self, session: requests.Session):
        self.session = session
        self.cache: dict[str, object] = {}
        self.lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self.lock:
            have = base in self.cache
            rp = self.cache.get(base)
        if not have:
            rp = None
            try:
                r = self.session.get(base + "/robots.txt", timeout=(4, 8),
                                     allow_redirects=False)
                if r.status_code == 200 and "html" not in r.headers.get("Content-Type", "").lower():
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except (requests.ConnectionError, requests.Timeout):
                rp = self.DEAD
            except Exception:
                rp = None
            with self.lock:
                self.cache[base] = rp
        if rp == self.DEAD:
            return False
        if rp is None:
            return True
        try:
            return rp.can_fetch("*", url)
        except Exception:
            return True


class Fetcher:
    def __init__(self):
        self.session = make_session()
        self.robots = RobotsCache(self.session)
        self.timings = None  # optional stage-timing collector (set by the job)

    def _time(self, stage: str, t0: float):
        if self.timings is not None:
            self.timings.add(stage, time.perf_counter() - t0)

    def fetch(self, url: str, max_bytes: int = 2_000_000) -> tuple[bytes | None, str, str]:
        """Return (body or None, final URL, failure reason). Redirects are
        followed manually so every hop passes the SSRF guard."""
        current = url
        deadline = time.monotonic() + config.FETCH_DEADLINE
        try:
            for _ in range(MAX_REDIRECTS + 1):
                reason = url_block_reason(current)
                if reason:
                    return None, current, f"blocked ({reason})"
                with self.session.get(current, timeout=FETCH_TIMEOUT, stream=True,
                                      allow_redirects=False) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location", "")
                        if not loc:
                            return None, current, "redirect without location"
                        current = urljoin(current, loc.strip())
                        continue
                    if r.status_code >= 400:
                        return None, current, f"HTTP {r.status_code}"
                    ctype = r.headers.get("Content-Type", "").lower()
                    if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
                        return None, current, f"non-HTML content ({ctype.split(';')[0]})"
                    chunks, size = [], 0
                    for chunk in r.iter_content(65536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > max_bytes:
                            break
                        if time.monotonic() > deadline:
                            # slow-drip body: keep what arrived (the head of
                            # the page holds name/contact data) and free the worker
                            if size < 2048:
                                return None, current, "timeout (slow response)"
                            break
                    return b"".join(chunks), current, ""
            return None, current, "too many redirects"
        except requests.exceptions.SSLError:
            return None, current, "invalid SSL certificate"
        except requests.exceptions.ConnectionError:
            return None, current, "connection/DNS error"
        except requests.exceptions.Timeout:
            return None, current, "timeout"
        except requests.RequestException as exc:
            return None, current, f"request error: {str(exc)[:80]}"
        except Exception as exc:  # defensive: one bad URL must not kill the run
            return None, current, f"unexpected error: {str(exc)[:80]}"

    def fetch_raw(self, url: str) -> tuple[bytes | None, str, str]:
        """SSRF check + robots.txt + fetch, WITHOUT parsing (parsing happens
        off the GIL in the analysis process pool). Returns (body or None,
        final URL, failure reason). Every fetch is logged with timing."""
        reason = url_block_reason(url)
        if reason:
            return None, url, f"blocked ({reason})"
        t0 = time.perf_counter()
        allowed = self.robots.allowed(url)
        self._time("robots", t0)
        if not allowed:
            return None, url, "robots.txt disallows (or host unreachable)"
        t_start = time.time()
        t0 = time.perf_counter()
        raw, final_url, reason = self.fetch(url)   # session retries read/5xx once
        self._time("fetch", t0)
        crawl_log.info("fetch url=%s start=%s end=%s dur=%dms status=%s bytes=%d",
                       url[:120], time.strftime("%H:%M:%S", time.localtime(t_start)),
                       time.strftime("%H:%M:%S"), (time.perf_counter() - t0) * 1000,
                       "OK" if raw is not None else reason, len(raw or b""))
        return raw, final_url, reason

    def fetch_page(self, url: str) -> tuple[BeautifulSoup | None, str, str, str]:
        """Fetch and parse. Returns (soup or None, raw text, final URL, reason)."""
        reason = url_block_reason(url)
        if reason:
            return None, "", url, f"blocked ({reason})"
        t0 = time.perf_counter()
        allowed = self.robots.allowed(url)
        self._time("robots", t0)
        if not allowed:
            return None, "", url, "robots.txt disallows (or host unreachable)"
        t0 = time.perf_counter()
        raw, final_url, reason = self.fetch(url)
        self._time("fetch", t0)
        if raw is None:
            return None, "", final_url, reason
        t0 = time.perf_counter()
        try:
            soup = BeautifulSoup(raw, BS_PARSER)
        except Exception:
            return None, "", final_url, "unparseable HTML"
        self._time("parse", t0)
        return soup, raw.decode("utf-8", "replace"), final_url, ""


def find_contact_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    base_root = root_domain(base_url)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        txt = a.get_text(" ")
        if CONTACT_LINK_RE.search(href) or CONTACT_LINK_RE.search(txt):
            full = urljoin(base_url, href).split("#")[0]
            if not full.lower().startswith("http"):
                continue
            if root_domain(full) != base_root:
                continue
            if full.rstrip("/") == base_url.rstrip("/") or full in out:
                continue
            if re.search(r"\.(pdf|jpg|png|zip|docx?)$", full, re.I):
                continue
            out.append(full)
    out.sort(key=lambda u: 0 if "contact" in u.lower() else 1)
    return out


def external_links(soup: BeautifulSoup, page_url: str, skip_check,
                   cap: int = MAX_CANDIDATES_PER_PAGE) -> list[str]:
    """Outbound links to distinct external domains (directory mining)."""
    page_root = root_domain(page_url)
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        rd = root_domain(href)
        if not rd or rd == page_root or rd in seen or skip_check(href):
            continue
        seen.add(rd)
        out.append(href)
        if len(out) >= cap:
            break
    return out
