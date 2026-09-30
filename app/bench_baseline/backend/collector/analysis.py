"""Off-GIL page analysis.

Parsing HTML into a BeautifulSoup tree and running extraction is pure-Python
CPU work (~170 ms per real page). Done inside crawl threads it serialises on
the GIL: with 32 threads the measured wall time per parse was 3.8 s. Here the
fetched bytes are handed to a process pool, so parsing runs on real cores
while the crawl threads only wait on network I/O.

The same extraction code runs either way; ANALYZE_PROCESSES=0 (or a broken
pool) falls back to analysing in the calling thread.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from bs4 import BeautifulSoup

from .. import config
from .crawler import BS_PARSER, external_links, find_contact_links
from .extractor import extract_company
from .normalize import clean_text
from .validator import BLOCK_TITLE_RE, is_skip_domain

log = logging.getLogger("analysis")

ANALYZE_TIMEOUT = 30.0

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_disabled = False


def analyze_html(raw: bytes, url: str, extract: bool, contact_links: bool,
                 mine: bool, mine_cap: int) -> dict:
    """Parse one page and return plain data (picklable). Runs in a worker
    process. Downloaded content is only parsed, never executed."""
    t0 = time.perf_counter()
    soup = BeautifulSoup(raw, BS_PARSER)
    t_parse = time.perf_counter() - t0
    title = clean_text(soup.title.get_text()) if soup.title else ""
    out = {"title": title, "parse_s": t_parse, "extract_s": 0.0}
    if mine:
        out["external_links"] = external_links(soup, url, is_skip_domain, cap=mine_cap)
    if BLOCK_TITLE_RE.search(title):
        out["blocked"] = True
        return out
    if extract:
        t1 = time.perf_counter()
        ex = extract_company(soup, raw.decode("utf-8", "replace"), url)
        out.update(name=ex.name, email=ex.email, phone=ex.phone,
                   address=ex.address, services=ex.services)
        out["extract_s"] = time.perf_counter() - t1
    if contact_links:
        out["contact_links"] = find_contact_links(soup, url)[:3]
    return out


def _get_pool() -> ProcessPoolExecutor | None:
    global _pool
    if _disabled or config.ANALYZE_PROCESSES <= 0:
        return None
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=config.ANALYZE_PROCESSES)
        return _pool


def warm_up():
    """Start the worker processes ahead of the first page."""
    pool = _get_pool()
    if pool is not None:
        for _ in range(config.ANALYZE_PROCESSES):
            pool.submit(time.sleep, 0)


def analyze(raw: bytes, url: str, extract: bool = True, contact_links: bool = True,
            mine: bool = False, mine_cap: int = 15) -> dict:
    """Analyse a page off the GIL; never raises. On failure returns
    {"error": reason}."""
    global _disabled, _pool
    args = (raw, url, extract, contact_links, mine, mine_cap)
    pool = _get_pool()
    if pool is None:
        try:
            return analyze_html(*args)
        except Exception as exc:
            return {"error": f"unparseable HTML ({type(exc).__name__})"}
    try:
        return pool.submit(analyze_html, *args).result(timeout=ANALYZE_TIMEOUT)
    except TimeoutError:
        return {"error": "page analysis timed out"}
    except BrokenProcessPool:
        log.warning("analysis process pool broke; falling back to in-thread parsing")
        with _pool_lock:
            _disabled = True
            _pool = None
        return analyze(raw, url, extract, contact_links, mine, mine_cap)
    except Exception as exc:
        return {"error": f"unparseable HTML ({type(exc).__name__})"}
