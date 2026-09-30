"""FastAPI backend for the Business Data Collector demo.

Run from the app/ directory:
    python -m uvicorn backend.main:app --port 8100
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config
from .collector.categories import CATEGORIES, SUMMARY_FILE, suggested_keywords
from .collector.engine import ACTIVE_STATUSES, CollectionJob, StateStore
from .collector.exporter import write_category_file, write_master_summary

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
# Also log to a rotating file: the app may run hidden (no console), and
# production monitoring needs a persistent log anyway. Never contains secrets.
try:
    from logging.handlers import RotatingFileHandler
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    _fh = RotatingFileHandler(os.path.join(config.OUTPUT_DIR, "app.log"),
                              maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except OSError:
    pass
log = logging.getLogger("api")

app = FastAPI(title="Business Data Collector Demo", docs_url=None,
              redoc_url=None, openapi_url=None)

# ---------------------------------------------------------------------------
# Security middleware: request size limit, per-IP rate limiting, optional
# shared-token auth, safe error responses, security headers, request logging.
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 64 * 1024
RATE_LIMITS = {"api": (240, 60.0), "collect": (10, 60.0)}  # (requests, window s)
_rl: dict[str, deque] = defaultdict(deque)
_rl_lock = threading.Lock()

SECURITY_HEADERS = {
    "Content-Security-Policy":
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _rate_limited(key: str, bucket: str) -> bool:
    limit, window = RATE_LIMITS[bucket]
    now = time.time()
    with _rl_lock:
        q = _rl[f"{bucket}:{key}"]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


@app.middleware("http")
async def security_middleware(request, call_next):
    path = request.url.path
    ip = request.client.host if request.client else "?"
    if path.startswith("/api") and request.method != "OPTIONS":
        try:
            if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request body too large"}, 413)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, 400)
        bucket = "collect" if path == "/api/collect" else "api"
        if _rate_limited(ip, bucket):
            log.warning("rate limit exceeded: ip=%s path=%s", ip, path)
            return JSONResponse({"detail": "Too many requests, slow down"}, 429,
                                headers={"Retry-After": "30"})
        token = config.auth_token()
        if token and path != "/api/health":
            supplied = request.headers.get("x-auth-token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                log.warning("auth failure: ip=%s path=%s", ip, path)
                return JSONResponse({"detail": "Access code required"}, 401)
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception:
        # Never leak stack traces or internal paths to clients.
        log.exception("unhandled error on %s %s", request.method, path)
        response = JSONResponse({"detail": "Internal server error"}, 500)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if config.force_hsts():
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    if path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-store")
    log.info("%s %s -> %s (%dms) ip=%s", request.method, path,
             response.status_code, (time.time() - t0) * 1000, ip)
    return response


# CORS is added after the security middleware so it wraps it (outermost):
# even 401/429 responses carry CORS headers and preflights pass untouched.
# Origins are locked to local defaults unless APP_ALLOWED_ORIGINS overrides
# them for a production domain — never "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.allowed_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Auth-Token"],
    # Chrome's Private Network Access: file:// pages calling 127.0.0.1 send a
    # preflight that must be acknowledged or the request is blocked.
    allow_private_network=True,
)

_state = StateStore.load(config.STATE_PATH)
_job: CollectionJob | None = None
_job_lock = threading.Lock()

SAFE_FILE_RE = re.compile(r"^[\w\-. ]+\.xlsx$")


class CollectRequest(BaseModel):
    category: str                      # fixed/custom slug, or "__custom__"
    custom_category: str = Field(default="", max_length=60)
    keywords: str = ""
    country: str = Field(default="", max_length=60)
    state: str = Field(default="", max_length=60)   # state / region
    city: str = Field(default="", max_length=60)
    location: str = ""                 # legacy free-text location (still works)
    target: int = Field(default=config.DEFAULT_TARGET, ge=1, le=config.MAX_TARGET)
    provider: str = "serper"
    max_queries: int = Field(default=config.DEFAULT_MAX_QUERIES, ge=1,
                             le=config.MAX_MAX_QUERIES)


def _per_category_stats() -> dict:
    """Live per-category card data from the central category registry (the
    fixed ten plus any custom categories), zeros included."""
    cats = _state.all_categories()
    with _state.lock:
        return {c: {
            "display": cdef["display"],
            "file": cdef["file"],
            "custom": c not in CATEGORIES,
            "suggested": suggested_keywords(c),
            "count": len(_state.records.get(c, [])),
            "emails": sum(1 for r in _state.records.get(c, []) if r.get("Business Email")),
            "websites": sum(1 for r in _state.records.get(c, []) if r.get("Official Website")),
        } for c, cdef in cats.items()}


@app.get("/api/health")
def health():
    """Unauthenticated liveness probe for deployment health checks."""
    return {"status": "ok", "version": config.APP_VERSION,
            "auth_required": bool(config.auth_token())}


@app.get("/api/config")
def get_config():
    per_cat = _per_category_stats()
    return {
        "categories": [{"slug": c, **d} for c, d in per_cat.items()],
        "providers": ["serper"],
        "serper_key_present": bool(config.serper_api_key()),
        "defaults": {"target": config.DEFAULT_TARGET,
                     "max_target": config.MAX_TARGET,
                     "target_choices": config.TARGET_CHOICES,
                     "max_queries": config.DEFAULT_MAX_QUERIES,
                     "credits_per_query": 1 + config.PLACES_PAGES_PER_QUERY},
    }


@app.post("/api/collect")
def start_collect(req: CollectRequest):
    global _job
    category = req.category
    if category == "__custom__":
        name = " ".join(req.custom_category.split())
        if len(name) < 3:
            raise HTTPException(400, "Enter a custom category name (3+ characters).")
        category = _state.ensure_custom(name)
    elif category not in _state.all_categories():
        raise HTTPException(400, "unknown category")
    if req.provider not in ("serper",):
        raise HTTPException(400, "unknown search provider")
    if not config.serper_api_key():
        raise HTTPException(400, "SERPER_API_KEY is not configured. "
                                 "Copy .env.example to app/.env and set your key.")
    geo = {"city": req.city.strip(), "state": req.state.strip(),
           "country": req.country.strip()}
    # Query location: "City, State, Country" from whichever parts are given.
    location = ", ".join(p for p in (geo["city"], geo["state"], geo["country"]) if p) \
        or req.location.strip()
    with _job_lock:
        if _job is not None and _job.status in ACTIVE_STATUSES:
            raise HTTPException(409, "A collection is already running. Stop it first.")
        kws = [k for k in req.keywords.split(",") if k.strip()]
        _job = CollectionJob(_state, category, kws, location,
                             req.target, req.provider, req.max_queries, geo=geo)
        _job.start()
    return _job.snapshot()


@app.post("/api/stop")
def stop_collect():
    if _job is None:
        raise HTTPException(404, "no job")
    _job.stop()
    return {"ok": True}


@app.get("/api/status")
def status():
    per_cat = _per_category_stats()
    if _job is None:
        return {"job": None, "per_category": per_cat}
    return {"job": _job.snapshot(), "per_category": per_cat}


@app.get("/api/records")
def records(category: str, limit: int = 50):
    if category not in _state.all_categories():
        raise HTTPException(400, "unknown category")
    with _state.lock:
        rows_all = _state.records.get(category, [])
        total = len(rows_all)
        rows = list(rows_all)[-limit:]
    return {"category": category, "total": total, "records": rows[::-1]}


@app.get("/api/files")
def files():
    out = []
    if os.path.isdir(config.OUTPUT_DIR):
        for name in sorted(os.listdir(config.OUTPUT_DIR)):
            if name.endswith(".xlsx") and SAFE_FILE_RE.match(name):
                p = os.path.join(config.OUTPUT_DIR, name)
                out.append({"name": name, "size": os.path.getsize(p),
                            "modified": os.path.getmtime(p)})
    return {"files": out}


@app.get("/api/download/{name}")
def download(name: str):
    known = {d["file"] for d in _state.all_categories().values()} | {SUMMARY_FILE}
    if name not in known:
        raise HTTPException(404, "unknown file")
    path = os.path.join(config.OUTPUT_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "file not generated yet")
    return FileResponse(
        path, filename=name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class DeleteCategoryRequest(BaseModel):
    category: str = Field(max_length=80)


@app.post("/api/category/delete")
def delete_category(req: DeleteCategoryRequest):
    """Delete one category's collected data (and, for a custom category, the
    category itself and its Excel file)."""
    if _job is not None and _job.status in ACTIVE_STATUSES:
        raise HTTPException(409, "Stop the running collection first.")
    cats = _state.all_categories()
    if req.category not in cats:
        raise HTTPException(400, "unknown category")
    display = cats[req.category]["display"]
    removed, is_custom, fname = _state.delete_category(req.category)
    _state.save(config.STATE_PATH)
    path = os.path.join(config.OUTPUT_DIR, fname)
    try:
        if is_custom:
            if os.path.exists(path):
                os.remove(path)
        else:
            write_category_file(display, fname, [], config.OUTPUT_DIR)
    except OSError:
        pass  # e.g. the file is open in Excel; data is already deleted
    cats = _state.all_categories()
    with _state.lock:
        records = {c: list(rs) for c, rs in _state.records.items()}
        stats = {c: dict(s) for c, s in _state.stats.items()}
    write_master_summary(cats, records, stats, config.OUTPUT_DIR)
    return {"ok": True, "removed": removed, "was_custom": is_custom,
            "display": display}


@app.post("/api/reset")
def reset():
    """Wipe the demo checkpoint (records + dedup registry). Excel files stay."""
    global _state
    if _job is not None and _job.status in ACTIVE_STATUSES:
        raise HTTPException(409, "stop the running collection first")
    _state = StateStore()
    try:
        os.remove(config.STATE_PATH)
    except OSError:
        pass
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(config.FRONTEND_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(config.FRONTEND_DIR, "favicon.svg"),
                        media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Single-instance guard: two copies sharing the same output folder would
# silently overwrite each other's checkpoints (lost progress, not corruption
# - saves are atomic). Warn loudly instead of refusing, so dev --reload
# workflows keep working.
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    import ctypes
    if pid <= 0 or os.name != "nt":
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@app.on_event("startup")
def _instance_guard():
    lock = os.path.join(config.OUTPUT_DIR, "app.lock")
    try:
        if os.path.exists(lock):
            with open(lock, "r", encoding="utf-8") as fh:
                other = int(fh.read().strip() or 0)
            if other and other != os.getpid() and _pid_alive(other):
                log.warning(
                    "ANOTHER INSTANCE (PID %d) is already running with this "
                    "data folder. Run only ONE copy of the app - a second one "
                    "can overwrite the first one's collected progress. Use "
                    "start_app.bat / http://127.0.0.1:8100 and close this one.",
                    other)
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except (OSError, ValueError):
        pass


@app.on_event("shutdown")
def _instance_release():
    lock = os.path.join(config.OUTPUT_DIR, "app.lock")
    try:
        with open(lock, "r", encoding="utf-8") as fh:
            if int(fh.read().strip() or 0) == os.getpid():
                os.remove(lock)
    except (OSError, ValueError):
        pass
