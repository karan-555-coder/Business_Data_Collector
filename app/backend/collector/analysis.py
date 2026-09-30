"""Off-GIL page analysis.

Parsing HTML (dom.py, lxml) and running extraction is CPU work (~30 ms per
real page; ~200 ms with the former BeautifulSoup tree). Done inside crawl
threads it serialises on
the GIL: with 32 threads the measured wall time per parse was 3.8 s. Here the
fetched bytes are handed to a process pool, so parsing runs on real cores
while the crawl threads only wait on network I/O.

The same extraction code runs either way; ANALYZE_PROCESSES=0 (or a broken
pool) falls back to analysing in the calling thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from .. import config
from . import dom
from .crawler import external_links, find_contact_links
from .extractor import extract_company
from .normalize import clean_text
from .validator import BLOCK_TITLE_RE, is_skip_domain

log = logging.getLogger("analysis")

ANALYZE_TIMEOUT = 30.0

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_disabled = False
_breaks: list[float] = []          # times the pool broke (a worker died)
MAX_POOL_BREAKS = 3                # per 10 minutes before in-thread fallback


def analyze_html(raw: bytes, url: str, extract: bool, contact_links: bool,
                 mine: bool, mine_cap: int) -> dict:
    """Parse one page and return plain data (picklable). Runs in a worker
    process. Downloaded content is only parsed, never executed."""
    t0 = time.perf_counter()
    doc = dom.parse(raw)
    t_parse = time.perf_counter() - t0
    title = clean_text(dom.title(doc) or "")
    out = {"title": title, "parse_s": t_parse, "extract_s": 0.0}
    if mine:
        out["external_links"] = external_links(doc, url, is_skip_domain, cap=mine_cap)
    if BLOCK_TITLE_RE.search(title):
        out["blocked"] = True
        return out
    if extract:
        t1 = time.perf_counter()
        ex = extract_company(doc, raw.decode("utf-8", "replace"), url)
        out.update(name=ex.name, email=ex.email, phone=ex.phone,
                   address=ex.address, services=ex.services)
        out["extract_s"] = time.perf_counter() - t1
    if contact_links:
        out["contact_links"] = find_contact_links(doc, url)[:3]
    return out


def _exit_with_parent(parent_pid: int):
    """Pool-process initializer: exit as soon as the process that owns the
    pool is gone. Without it, killing the collection worker (crash, task
    manager) left its parsing processes running forever - and every
    automatic worker restart started a fresh set next to the orphans.
    Ctrl+C is ignored here: the worker decides when parsing stops."""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    def watch():
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
            if handle:
                k32.WaitForSingleObject(handle, 0xFFFFFFFF)            # INFINITE
                os._exit(0)
        while True:                       # POSIX (or OpenProcess refused)
            time.sleep(2.0)
            if os.getppid() != parent_pid:
                os._exit(0)
    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def _get_pool() -> ProcessPoolExecutor | None:
    global _pool
    if _disabled or config.ANALYZE_PROCESSES <= 0:
        return None
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=config.ANALYZE_PROCESSES,
                                        initializer=_exit_with_parent,
                                        initargs=(os.getpid(),))
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
        # A worker process died (e.g. killed, out of memory). Parsing in the
        # crawl threads instead would serialize every job on the GIL, so the
        # pool is rebuilt; only repeated breakage falls back to in-thread.
        with _pool_lock:
            if _pool is pool:
                now = time.time()
                _breaks[:] = [t for t in _breaks if now - t < 600] + [now]
                _pool = None
                if len(_breaks) > MAX_POOL_BREAKS:
                    _disabled = True
                    log.error("analysis process pool broke %d times in 10 min; "
                              "falling back to in-thread parsing", len(_breaks))
                else:
                    log.warning("analysis process pool broke; starting a new one")
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        return analyze(raw, url, extract, contact_links, mine, mine_cap)
    except Exception as exc:
        return {"error": f"unparseable HTML ({type(exc).__name__})"}
