"""App configuration. The Serper key comes ONLY from the environment or app/.env
(never hardcoded, never logged, never sent to the frontend)."""

from __future__ import annotations

import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app
OUTPUT_DIR = os.path.join(APP_DIR, "output")
STATE_PATH = os.path.join(OUTPUT_DIR, "demo_state.json")
FRONTEND_DIR = os.path.join(APP_DIR, "frontend")

# Demo cost-control defaults (each Serper search or places page = 1 credit).
DEFAULT_TARGET = 100
MAX_TARGET = 2000
TARGET_CHOICES = [20, 50, 100, 250, 500, 1000, 1500, 2000]
DEFAULT_MAX_QUERIES = 12
MAX_MAX_QUERIES = 400            # large targets need hundreds of queries
YIELD_EXHAUSTED_STREAK = 10      # stop after N consecutive zero-yield queries
ORGANIC_RESULTS_PER_QUERY = 10   # one Serper page
PLACES_PAGES_PER_QUERY = 1       # one Serper page (10 places)
DIRECTORY_CANDIDATES_CAP = 15    # links mined per directory/list page


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def _load_dotenv() -> None:
    """Tiny .env loader (KEY=VALUE lines); does not override real env vars."""
    path = os.path.join(APP_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                # drop trailing "  # comment" (as used in .env.example)
                value = value.split(" #", 1)[0].split("\t#", 1)[0]
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


# Must run before the tuning constants below read os.environ.
_load_dotenv()


# Parallel site fetches. Tune without code changes via SCRAPER_CONCURRENCY
# (or the older APP_CRAWL_WORKERS) in .env, range 4..100. Crawling is
# I/O-bound; robots.txt and per-site page limits are always respected.
CRAWL_WORKERS = _int_env("SCRAPER_CONCURRENCY",
                         _int_env("APP_CRAWL_WORKERS", 48, 4, 100), 4, 100)
FETCH_TIMEOUT = (5, 12)          # (connect, read) - dead hosts fail fast
EXPORT_MIN_INTERVAL = 30.0       # mid-run Excel regeneration (final export
                                 # always happens; the state JSON is the
                                 # crash-safe checkpoint)
STATE_SAVE_INTERVAL = 5.0

# HTML parsing/extraction runs in worker processes (off the GIL).
# 0 = parse inside crawl threads (old behaviour).
ANALYZE_PROCESSES = _int_env("ANALYZE_PROCESSES",
                             max(1, min(8, (os.cpu_count() or 4) - 2)), 0, 16)

# ---- Serper client tuning (all overridable in .env) ------------------------
SERPER_CONCURRENCY = _int_env("SERPER_CONCURRENCY", 6, 1, 16)  # requests in flight
SERPER_RPS = _int_env("SERPER_RPS", 8, 1, 50)          # adaptive ceiling
SEARCH_PREFETCH = _int_env("SEARCH_PREFETCH", 3, 1, 8)  # queries searched ahead
                                 # of the one being crawled
SEARCH_BACKLOG_LIMIT = 2         # x CRAWL_WORKERS: queued crawl tasks above
                                 # which no new searches are launched
PLACES_MAX_PAGE = 3              # follow-up Places pages for productive queries
def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


# Spend safety valve per run: credits <= max(2 x max_queries, target x this).
# The planner keeps generating NEW searches (wider tiers) until the target is
# reached; this cap is the only spend-based stop. 0 disables it. Measured
# yield is ~2-3 valid records per credit, so 1.5 leaves ample headroom.
MAX_CREDITS_PER_RECORD = _float_env("MAX_CREDITS_PER_RECORD", 1.5, 0.0, 20.0)
SERPER_MAX_ATTEMPTS = _int_env("SERPER_MAX_ATTEMPTS", 4, 1, 8)  # per request
SEARCH_RETRIES = _int_env("SEARCH_RETRIES", 2, 0, 5)  # re-queues of a failed search
SERPER_TIMEOUT = (5, 15)         # (connect, read): a 35 s response is a bug
SEARCH_RESOLVE_TIMEOUT = 12.0    # max seconds the pipeline waits on a search;
                                 # slower ones finish in background + cache
SEARCH_CACHE_TTL = 24 * 3600     # request-level dedup cache lifetime
SEARCH_CACHE_MAX = 300
MAX_CONNECTIONS = _int_env("MAX_CONNECTIONS", 100, 16, 200)
KEEPALIVE_CONNECTIONS = _int_env("KEEPALIVE_CONNECTIONS", 100, 16, 200)
PER_DOMAIN_CONCURRENCY = 1       # by design: one page at a time per website
FETCH_DEADLINE = 25.0            # total seconds per page (slow-drip bodies);
                                 # read/5xx retries: crawler.make_session

# ---- Collection watchdog (fault tolerance) ---------------------------------
STUCK_TASK_S = _int_env("STUCK_TASK_S", 180, 5, 3600)       # abandon a crawl task
WATCHDOG_STALL_S = _int_env("WATCHDOG_STALL_S", 60, 5, 3600)   # no new record ->
                                 # diagnose, retry, refresh search strategy
WATCHDOG_BOTTLENECK_S = _int_env("WATCHDOG_BOTTLENECK_S", 300, 10, 7200)
MAX_CONTROLLER_ERRORS = 25       # consecutive controller-loop failures -> FAILED

APP_VERSION = "1.0.0"


def serper_api_key() -> str:
    return os.environ.get("SERPER_API_KEY", "").strip()


def auth_token() -> str:
    """Optional shared access code. When set (APP_AUTH_TOKEN), every /api
    request must carry it in the X-Auth-Token header. Recommended whenever the
    app is reachable beyond this machine (network launcher, reverse proxy)."""
    return os.environ.get("APP_AUTH_TOKEN", "").strip()


def allowed_origins() -> list[str]:
    """CORS origins. Local defaults; override with APP_ALLOWED_ORIGINS
    (comma-separated) behind a production domain. Never '*'."""
    env = os.environ.get("APP_ALLOWED_ORIGINS", "").strip()
    if env:
        return [o.strip() for o in env.split(",") if o.strip()]
    return ["null", "http://127.0.0.1:8100", "http://localhost:8100"]


def force_hsts() -> bool:
    """Send Strict-Transport-Security (set APP_FORCE_HTTPS=1 once the app is
    served over HTTPS via a reverse proxy)."""
    return os.environ.get("APP_FORCE_HTTPS", "").strip() in ("1", "true", "yes")
