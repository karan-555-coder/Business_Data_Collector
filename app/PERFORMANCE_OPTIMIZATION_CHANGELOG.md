# Performance Optimization Changelog

Date: 2026-10-01 · Scope: backend performance and resource use on Render Free
(0.1 CPU / 512 MB RAM) · API contracts, UI, scraped fields and Excel format
unchanged.

Every number in this document was measured as described in
[10. Testing / Benchmarking](#10-testing--benchmarking). Where something was
not measured, the text says so.

---

## Project Overview

**What the application does.** The Business Data Collector finds businesses for a
category and location (Serper.dev Google Search + Google Places), crawls their
public websites, extracts contact data (name, email, phone, address, services),
validates and deduplicates the results, and exports one Excel file per category
plus a master summary.

```
SEARCH -> DISCOVER -> CRAWL -> EXTRACT -> VALIDATE -> DEDUPLICATE -> STORE -> EXCEL
```

**How the scraping flow works.**

1. The browser posts `POST /api/collect`. The API process forwards it over a
   pipe to the **collection worker process**, which queues or starts a
   `CollectionJob` and returns the job (with its `id`) at once.
2. The job's controller thread (`CollectionJob._control_loop`) asks the
   `DiscoveryPlanner` for the next searches and sends them to Serper in a search
   thread pool. Results are processed the moment they arrive
   (`_search_task` → `_process_organic` / `_process_places`). Already-known
   domains are dropped before any crawl.
3. New sites go to the crawl thread pool (`crawl_site`, `enrich_place`). The
   `Fetcher` checks the SSRF guard and robots.txt, fetches the page, and the
   page is parsed and extracted (`analysis.analyze` → `dom` + `extractor`).
   At most two contact/about pages are fetched per site.
4. Each candidate record is validated (`validator.validate`), checked against
   the dedup registry (`DedupRegistry.check_and_add`) and appended to the
   in-memory state (`StateStore.append_record`).
5. A background saver thread writes the JSON checkpoint every 5 s and the Excel
   files every `EXPORT_MIN_INTERVAL`. The job's final export is verified by
   re-reading the workbook.

**Where things live.**

| Concern | Location |
|---|---|
| API entry point (ASGI app, fast-path GET handlers, rate limits, auth) | `app/backend/main.py` |
| Production server runner (uvicorn options, `$PORT`) | `app/backend/serve.py` |
| API ↔ worker process supervision, RPC, cached status bundles | `app/backend/hub.py` |
| Collection worker process (jobs, status publisher, shutdown) | `app/backend/worker.py` |
| Multi-user job scheduling (queue, slots, per-user limits) | `app/backend/collector/jobs.py` |
| **Where scraping starts**: job controller, search, crawl, checkpoint | `app/backend/collector/engine.py` |
| HTTP session, robots.txt cache, page fetch, link discovery | `app/backend/collector/crawler.py` |
| Shared TLS context (new) | `app/backend/collector/tls.py` |
| HTML parse + extraction (in-thread or process pool) | `app/backend/collector/analysis.py`, `dom.py`, `extractor.py` |
| **Where results are processed/stored**: validation, dedup, state | `validator.py`, `deduplicator.py`, `engine.StateStore` |
| **Where Excel is generated** | `app/backend/collector/exporter.py` (called from `StateStore.export_categories`) |
| **Where Serper is used** | `app/backend/collector/search.py` (`SerperProvider`), planner in `discovery.py`, credit ledger in `credits.py` |
| Configuration and env vars | `app/backend/config.py`, `app/.env.example`, `render.yaml` |
| Frontend (single page, polling) | `app/frontend/index.html` (Vercel proxies `/api/*` to Render) |

---

## Optimization Summary

| Section | Problem | Change Made | Files Changed | Expected Effect |
|---------|---------|-------------|---------------|-----------------|
| 1. HTTP / SSL | requests 2.34 makes urllib3 build a new `SSLContext` and parse the 240 KB CA bundle on **every new TLS connection**: measured ~320 ms CPU each | One verified TLS context, loaded once and shared by every HTTPS pool (`SharedTLSAdapter`) | `collector/tls.py` (new), `crawler.py`, `search.py` | Measured on 40 real sites: **832–850 → 18–26 ms CPU per site**. Certificate checks unchanged |
| 2. Crawler | On a 0.1-CPU host the TLS cost starved the CPU, robots.txt probes timed out, and **live sites were classified as dead and skipped** | Fixed by section 1. robots.txt cache bounded for small hosts | `crawler.py`, `config.py` | Measured at 0.1 CPU, 500 URLs: records found **3 → 319**; false "dead" sites 491 → 67 (≈ the 65 expected) |
| 3. Excel / Storage | Category workbook (~0.5 s CPU for 2,000 rows) and master summary rewritten every 30 s **even when nothing changed**; each rewrite also made open browsers download the file again | Write only when content changed (content digest + file stat). Small-host mid-run refresh every 60 s | `exporter.py`, `engine.py`, `config.py`, `render.yaml` | No rewrites while idle or stalled. Mid-run Excel CPU at most halved on a small host. Output verified identical (170,125 cells) |
| 4. FastAPI / Job architecture | Already non-blocking; status bundle rescanned all ~10,000 records of every category whenever one record changed (2.5 ms, up to twice a second) | Per-category stats cache. Small-host status publish cadence 0.5 → 1 s | `engine.py`, `config.py`, `render.yaml` | Less worker CPU per status update; API contracts unchanged |
| 5. Reliability / Recovery | On Linux a group-wide SIGTERM (deploy/restart) could kill the worker before its final checkpoint | Worker ignores SIGTERM like SIGINT; the API stops it over the pipe. Hub escalates to `kill()` | `worker.py`, `hub.py` | Final checkpoint + Excel written on a graceful stop |
| 6. Serper | Already deduplicated, cached, rate-limited (verified) | Serper session uses the shared TLS context | `search.py` | Small (Serper connections are kept alive); no behavior change |
| 7. Frontend / API | Already server-paced with backoff and ETags (verified) | Poll cadence configurable (`POLL_MS_ACTIVE` / `POLL_MS_IDLE`). Fewer Excel rewrites mean fewer browser re-downloads | `config.py`, `.env.example` | Less traffic during runs; no UI change |
| 8. CPU / Memory | robots.txt cache could hold 20,000 parsed files (~20–400 MB) | `ROBOTS_CACHE_HOSTS` = 2,000 on small hosts | `config.py`, `crawler.py` | Bounded memory on 512 MB |
| 9. Render | Config verified; new knobs not declared | Declared `EXPORT_MIN_INTERVAL`, `STATUS_PUBLISH_INTERVAL`, `ROBOTS_CACHE_HOSTS` | `render.yaml`, `.env.example` | Explicit even if small-host detection fails |
| 10. Testing | No crawl-path benchmark with real HTTP status mix; no test for the new behavior | `tests/test_performance.py`, `tests/loadtest/bench_crawl.py` (real Fetcher vs. local HTTPS web, 0.1-CPU cap) | new files | Repeatable before/after numbers |

---

# 1. HTTP / SSL Optimization

## What was the problem?

`requests` 2.34.2 (the pinned version) passes the CA bundle **path** to every
connection pool (`HTTPAdapter.cert_verify` sets `conn.ca_certs`). urllib3 2.8 then
creates a fresh `SSLContext` and calls `load_verify_locations()` on every new TLS
connection (`_ssl_wrap_socket_and_match_hostname` → `ssl_wrap_socket`). With
OpenSSL 3, parsing certifi's 240 KB PEM bundle is slow:

| Operation (this machine, Python 3.12.10) | Cost |
|---|---|
| `create_urllib3_context()` | ~18 ms |
| `load_verify_locations(certifi)` | **~320 ms CPU** |
| Parsing one real page with lxml (for comparison) | ~30 ms |

A crawler opens a new connection for almost every site, so TLS setup cost about
ten times more CPU than the parsing work itself.

## What did you change?

- Added `SharedTLSAdapter`, an `HTTPAdapter` subclass that:
  - attaches one shared, pre-loaded `SSLContext` to every **verified HTTPS** pool
    (`build_connection_pool_key_attributes` adds `ssl_context`);
  - clears `ca_certs` / `ca_cert_dir` after `cert_verify`, so urllib3 does not
    re-parse the bundle into that context per connection.
- The crawler session (`make_session`) and the Serper session (`_serper_session`)
  mount this adapter. Retry policy, pool sizes and headers are unchanged.

## Which files changed?

- `app/backend/collector/tls.py` (new)
- `app/backend/collector/crawler.py`
- `app/backend/collector/search.py`

## Which functions/classes changed?

- New: `tls.shared_tls_context()`, `tls.SharedTLSAdapter`
  (`build_connection_pool_key_attributes`, `cert_verify`)
- `crawler.make_session()`: `HTTPAdapter` → `SharedTLSAdapter`
- `search._serper_session()`: `HTTPAdapter` → `SharedTLSAdapter`

## Why does this improve performance?

The CA bundle is parsed once per process instead of once per TLS connection. An
`SSLContext` is safe to share between threads, and the per-connection settings
urllib3 writes to it are identical on every call.

## Before

Each new HTTPS connection (homepage, contact page on another host, robots.txt
after a redirect) cost ~0.3 s of CPU before a single byte was sent. With 32
crawl threads on a 0.1-CPU host, those parses queued behind each other.

## After

One context (CA bundle loaded once) serves every connection. Verification is
unchanged: `CERT_REQUIRED` plus hostname matching. A custom CA bundle
(`verify="path"` / `REQUESTS_CA_BUNDLE`) or `verify=False` keeps requests' stock
behavior, because the adapter only takes over the `verify=True` case.

Verified live (badssl.com): expired, wrong-host, self-signed and untrusted-root
certificates are still rejected (`SSLError`). Valid sites still pass. The CA
bundle was loaded **once for 9 hosts**.

## Expected impact

Measured on 40 real public HTTPS sites (homepage + robots.txt, 8 threads, two
runs each):

| | CPU per site | Wall (40 sites) | Outcomes |
|---|---|---|---|
| Before | 832–850 ms | 9.9–12.6 s | 34–35 ok, 3× HTTP 403, 2–3 unreachable |
| After | **18–26 ms** | 5.0–5.2 s | 34 ok, 3× HTTP 403, 3 unreachable |

The outcome differences (±1 unreachable) occur between runs of the same version
and come from network variance.

## Trade-offs / risks

- These numbers come from Windows. Python's OpenSSL build on Render (Linux) was
  **not measured** here. OpenSSL 3 is known to be slow at loading PEM bundles on
  all platforms, but the exact per-connection cost on Render may differ.
- The adapter relies on two documented `HTTPAdapter` hooks
  (`build_connection_pool_key_attributes`, `cert_verify`). Re-run
  `tests/test_performance.py` and the badssl check after upgrading `requests`.
- A CA added to certifi after the process started is only seen after a restart.
  This is normal for long-lived processes.

---

# 2. Crawler Optimization

## What was the problem?

The crawler was already well designed: bounded thread pools, URL/domain dedup
before crawling, one page at a time per site, at most 2 contact pages,
`connect=0` retries for dead hosts, one retry for read/5xx failures, no Retry-After
honoring, a 25 s per-page deadline, a robots.txt probe that marks unreachable
hosts dead, and a stuck-task watchdog.

The benchmark exposed a **correctness problem caused by CPU starvation**. The
robots.txt probe has a 4 s connect / 8 s read timeout, and a timeout marks the
host "dead" for an hour. When TLS setup alone needed ~0.3 s of CPU per
connection and the host had only 0.1 CPU, 32 concurrent handshakes could not
finish in time. Live sites failed the probe, were classified as dead, and were
silently skipped (reported as `robots.txt disallows (or host unreachable)`).

## What did you change?

- The root cause is fixed by section 1. No crawler logic changed.
- `RobotsCache.MAX_HOSTS` now comes from `config.ROBOTS_CACHE_HOSTS`: 2,000 on a
  small host, 20,000 elsewhere (see section 8).

## Which files changed?

- `app/backend/collector/crawler.py`
- `app/backend/config.py`

## Which functions/classes changed?

- `crawler.RobotsCache.MAX_HOSTS`

## Why does this improve performance?

With TLS setup at ~20 ms instead of ~0.3–0.85 s, handshakes finish well inside
the probe timeouts even at 0.1 CPU. Sites are judged on their real behavior.

## Before

At 0.1 CPU the crawler fetched pages from only 7 of 100 sites and 11 of 500 sites.
Everything else was marked dead.

## After

At 0.1 CPU the "dead / disallowed" count matches what the simulated web
actually contains: dead and NXDOMAIN hosts plus sites whose robots.txt
disallows everything, about 13 %.

HTTP status handling was verified as follows (unchanged by design):

| Response | Behavior | Evidence (benchmark) |
|---|---|---|
| 403 / 404 | failed immediately, no retry | no repeated requests for these hosts |
| 429 | failed immediately, Retry-After ignored (hostile sites send hours) | — |
| 500/502/503/504 | one retry with 0.3 s backoff (urllib3) | `repeated_requests_at_server` = number of 503 sites |
| connect error / DNS failure | no retry; host cached as dead for 1 h | — |
| slow body | partial page kept after the 25 s deadline | — |

## Expected impact

Measured. See [10. Testing / Benchmarking](#10-testing--benchmarking): at 0.1 CPU
with 500 URLs, records found went from 3 to 319.

## Trade-offs / risks

- 429 responses are still not retried. Retrying would hold a crawl thread per
  site, and a site yields at most one record. This was a deliberate decision in the
  original code and is kept.
- The "timeout ⇒ dead host" heuristic remains sensitive to CPU starvation. If
  the host is overloaded by something else, it can misjudge sites again. The
  watchdog and the benchmark make that visible.

---

# 3. Excel / Storage Optimization

## What was the problem?

- `StateStore._saver_loop` re-exported the running job's category workbook and
  the master summary every `EXPORT_MIN_INTERVAL` (30 s). `CollectionJob.checkpoint()`
  marks the category for export every 5 s **whether or not records changed**,
  so files were rewritten even during stalls and credit pauses.
- Measured cost: **~470–480 ms CPU** for a 2,000-row category workbook (openpyxl
  XML serialization), ~14 ms for the master summary. On a 0.1-CPU host that is
  ~5 s of wall-clock time per export, competing with the crawl.
- Every rewrite changes the file's mtime. The page (since commit `12a9aa8`)
  then downloads the changed file again into IndexedDB, for each open browser.

## What did you change?

- `exporter._write_if_changed()`: builds the rows, hashes them (BLAKE2b over
  their JSON), and skips the write when the file on disk is the one this
  process last wrote from identical rows (same digest, same mtime and size).
  If a file was changed or deleted externally, or a save failed (e.g. file
  open in Excel), it is rewritten.
- `write_category_file(..., force=False)` and `write_master_summary()` use it.
  The final-export **verification retry** in `CollectionJob._finalize_outputs`
  passes `force=True`, so a file that disagrees with the records is always
  regenerated.
- `exporter._save` became `_save_locked`, which reports success (`bool`).
- `EXPORT_MIN_INTERVAL` is now an env variable: 60 s on a small host, 30 s
  elsewhere. The final export at job end always happens.

## Which files changed?

- `app/backend/collector/exporter.py`
- `app/backend/collector/engine.py`
- `app/backend/config.py`
- `render.yaml`, `app/.env.example`

## Which functions/classes changed?

- `exporter._save` → `exporter._save_locked` (returns `bool`)
- New: `exporter._digest`, `exporter._write_if_changed`, `exporter._written`
- `exporter.write_category_file` (new `force` parameter)
- `exporter.write_master_summary`
- `engine.StateStore.export_categories` (new `force` parameter)
- `engine.CollectionJob._finalize_outputs`
- `config.EXPORT_MIN_INTERVAL`

## Why does this improve performance?

Most periodic exports were redundant. Skipping them costs a few milliseconds
for the digest instead of ~0.5 s for the workbook. During a productive run on
a small host, the remaining rewrites happen at most every 60 s instead of 30 s.

## Before

Category workbook and summary rewritten every 30 s during a job, regardless of
changes. Each rewrite caused a re-download in every open browser.

## After

A file is written only when its content changed, and at most every 60 s on a
small host. Unchanged files keep their mtime, so browsers do not download them
again.

The output format is unchanged. Before/after workbooks for all 11 files of the
real checkpoint were compared cell by cell: 170,125 cells, including values,
data types, number formats, header font/fill/alignment, column widths, freeze
panes and sheet titles. All identical. First-write cost is unchanged
(2.65 s vs. 2.66 s for all 11 files).

## Expected impact

- Idle, stalled or credit-paused jobs: no Excel CPU at all (was ~0.5 s every 30 s
  for a 2,000-row category).
- Productive jobs on a small host: mid-run Excel CPU at most half of before.
- Fewer IndexedDB re-downloads in the browser.

These effects follow directly from the change. The steady-state savings during a
long real run were **not** measured separately.

## Trade-offs / risks

- On a small host, mid-run downloads can be up to 60 s old instead of 30 s. The
  final file is always exact. Set `EXPORT_MIN_INTERVAL=30` to restore.
- The digest cache lives in the worker process memory (`_written`). After a
  restart, the first export of each file is a full write, as before.
- **Not adopted:** openpyxl `write_only` mode. Measured only 7 % faster
  (467 → 435 ms), and its header cell styles did not compare equal.
- **Not changed:** the 13 MB JSON checkpoint every 5 s (measured 16–40 ms per
  save). It is the crash-recovery mechanism, and its cost was small next to the
  items above.

---

# 4. FastAPI / Job Architecture

## What was the problem?

The architecture already meets the requirements, verified in code and by the
smoke test:

- **Long jobs never run in request handlers.** They run in a separate worker
  process (`hub.py` / `worker.py`). `POST /api/collect` returns the job with
  its `id` immediately.
- **Hot GETs are served from memory** (`/api/status`, `/api/health`,
  `/api/config`, `/api/records`, `/api/files`) by plain ASGI handlers. Records
  have ETag/304 support.
- **`/api/health` is lightweight:** no worker RPC, no disk access.
- **Jobs are isolated:** each user has their own job, `MAX_ACTIVE_JOBS` slots,
  a FIFO queue (`MAX_QUEUED_JOBS`), and one job per category at a time.
- **Job states** (unchanged API contract):

  | Requested state | This app's status |
  |---|---|
  | QUEUED | `queued` |
  | RUNNING | `pending`, `running`, `recovering` (a component is being retried), `waiting` (Serper credits exhausted, resumes automatically) |
  | COMPLETED | `completed` (target reached), `exhausted` (search space or credit cap used up; everything found is saved) |
  | FAILED | `failed` |
  | CANCELLED | `stopped` |

One real cost remained. The worker publishes a status bundle every 0.5 s.
Whenever any record was added, the bundle recomputed
`StateStore.category_stats()` by rescanning **every record of every category**
(~10,000 records in the real checkpoint): measured **2.5 ms** each time.

## What did you change?

- `StateStore.category_stats()` caches stats **per category**, keyed by that
  category's `cat_rev`. Only the category that changed (the running job's) is
  recounted. Every code path that changes records already calls
  `touch(category)`, which bumps `cat_rev`.
- `STATUS_PUBLISH_INTERVAL` is an env variable: 1.0 s on a small host
  (browsers poll every 3 s), 0.5 s elsewhere.

## Which files changed?

- `app/backend/collector/engine.py`
- `app/backend/config.py`, `render.yaml`, `app/.env.example`

## Which functions/classes changed?

- `engine.StateStore.__init__` (new `_cat_stats`)
- `engine.StateStore.category_stats`
- `config.STATUS_PUBLISH_INTERVAL`

## Why does this improve performance?

Only the changed category is recounted (~1/10 of the work with the real
checkpoint), and on a small host status bundles are built half as often.

## Before

Up to 2 full rescans per second (~5 ms CPU/s) while a job adds records.

## After

At most 1 rescan of one category per second on a small host.

## Expected impact

A small, steady CPU saving in the worker process, where it competes with
crawling. Not measured end-to-end, but derived from the 2.5 ms measurement above.

## Trade-offs / risks

On a small host, status can lag by up to 1 s instead of 0.5 s. That is invisible
with a 3 s poll.

---

# 5. Reliability / Recovery

## What was the problem?

Already in place (verified, unchanged):

- Atomic JSON checkpoint every 5 s (temp file + fsync + rename). A worker crash
  loses at most ~5 s of work.
- Failure isolation per request, search, page and task. The controller loop
  retries with backoff.
- The worker process is supervised: it restarts after a crash and running jobs
  resume from the checkpoint. The smoke test measured a restart in 1.6 s, with
  the job resumed at 330/400 after crashing at 343; records in that last
  checkpoint window are re-collected.
- Bounded queues: job queue 500, crawl backlog gated by search throttling,
  search cache 1,000, crawl memory 60,000 keys, log ring buffers 300 lines, job
  history 300.

**Gap:** on Linux, a host that sends SIGTERM to the whole process group (a deploy
or restart on Render) kills the worker process with the default handler. That
happens **before** the API can ask it to stop jobs and write the final
checkpoint. The supervisor could even restart the worker during shutdown. The
worker already ignored SIGINT for exactly this reason. SIGTERM was not covered.

## What did you change?

- `worker_main()` ignores SIGTERM on POSIX (like SIGINT / SIGBREAK). Shutdown is
  driven by the API: uvicorn handles SIGTERM, then the lifespan shutdown calls
  `hub.stop()`, which sends the `shutdown` RPC. The worker then stops its jobs,
  writes the final checkpoint and Excel files, and exits. If the API process dies,
  the pipe breaks and the worker still saves and exits (existing behavior).
- `WorkerHub.stop()` escalates with `kill()` instead of `terminate()` after the
  30 s grace period, because `terminate()` sends SIGTERM, which is now ignored.

## Which files changed?

- `app/backend/worker.py`
- `app/backend/hub.py`

## Which functions/classes changed?

- `worker.worker_main`
- `hub.WorkerHub.stop`

## Why does this improve performance?

This is a reliability fix: completed work is written to disk on every graceful
stop.

## Before

Group-wide SIGTERM ⇒ worker dies at once. Up to 5 s of records and the final
Excel export could be lost.

## After

Group-wide SIGTERM ⇒ the API shuts the worker down cleanly within the grace period.

## Expected impact

No lost progress on deploys or restarts of a running instance, as long as the
instance's disk survives (see section 9: on Render Free it does not).

## Trade-offs / risks

- If the API process hangs but stays alive, a SIGTERM sent **only** to the worker
  is ignored. `kill -9` / Render's final SIGKILL still works.
- Tested on Windows with Ctrl+Break: `tests/loadtest/shutdown_test.py`, clean
  exit, all 228 records in the checkpoint, no orphan processes. The Linux
  SIGTERM path was **not tested here** (no Linux host available).

---

# 6. Serper Optimization

## What was the problem?

Verified in code and by `tests/test_credits.py` (passes); no change needed:

- **Duplicate searches:** normalized request keys (`credits.ledger_key`); a
  persistent executed-query ledger, so a paid request is never sent again; a
  canonical-cell planner that drops trivial rephrasings.
- **Cache:** responses cached for 7 days (`SEARCH_CACHE_TTL`) and persisted in
  the checkpoint.
- **Concurrent duplicates:** in-flight dedup across all jobs (one wire request,
  others wait for it).
- **Timeouts:** `(5, 30)` s. Timed-out requests (possibly billed) are re-sent at
  most once.
- **Errors and rate limits:** exponential backoff with jitter for
  408/425/429/5xx and network errors. An adaptive account-wide rate limiter
  whose ceiling follows the account's advertised `rateLimit`. 401/402/403 and
  credit errors disable the provider; out-of-credits pauses the job and
  resumes after a top-up.

## What did you change?

- `_serper_session()` uses `SharedTLSAdapter` (section 1).

## Which files changed?

- `app/backend/collector/search.py`

## Which functions/classes changed?

- `search._serper_session`

## Why does this improve performance?

Serper connections are kept alive, so the gain is small: it only applies when
a connection is (re)opened.

## Before / After

Identical search behavior and credit use. Only (re)connections are cheaper.

## Expected impact

Minor. Serper latency and rate limits are external and cannot be changed in code.

## Trade-offs / risks

None identified.

---

# 7. Frontend / API Communication

## What was the problem?

Verified in `app/frontend/index.html`; no change needed:

- The server suggests the polling cadence (`poll_ms`: 3 s while the user's job
  runs, 10 s otherwise). Background tabs poll at most every 30 s. Failures back
  off exponentially (up to 30 s). Exactly one polling chain runs, with jitter.
- Status uses an incremental log cursor (`log_after`), so only new log lines are sent.
- The records table is not re-downloaded during a run. When idle it is refreshed
  only if the category count changed, and the server answers 304 when unchanged.

The remaining avoidable traffic came from Excel rewrites. Every rewrite changed
the file's mtime, and `refreshFiles()` (every 20 s during a run) then downloaded
the whole workbook again into IndexedDB.

## What did you change?

- No frontend code changed.
- Fewer Excel rewrites (section 3) mean fewer re-downloads.
- `POLL_MS_ACTIVE` / `POLL_MS_IDLE` are configurable via env. The page already
  follows `poll_ms` from `/api/status`, so no UI change is needed.

## Which files changed?

- `app/backend/config.py`, `app/.env.example`

## Which functions/classes changed?

- `config.POLL_MS_ACTIVE`, `config.POLL_MS_IDLE`

## Why does this improve performance?

Unchanged files keep their mtime, so the browser skips the download. Operators
can slow polling without a deploy of the frontend.

## Before / After

Before: up to 2 workbook downloads per 30 s per open browser during a run, even
when nothing changed. After: a download only when the file actually changed, at
most every 60 s on a small host.

## Expected impact

Less network traffic and fewer `/api/download` requests. Not measured
separately.

## Trade-offs / risks

None for the UI. Defaults are unchanged.

---

# 8. CPU / Memory Optimization

## What was the problem?

Profiled CPU consumers in the worker, with the real 13 MB checkpoint:

| Item | Measured cost | Action |
|---|---|---|
| New TLS connection (CA parse) | ~320 ms CPU | **Fixed** (section 1) |
| Excel export, 2,000-row category | ~480 ms CPU / 30 s | **Fixed**: only on change, 60 s on small host |
| `category_stats` full rescan | 2.5 ms, up to 2×/s | **Fixed**: per-category cache, 1×/s on small host |
| Checkpoint save (13 MB JSON, orjson) | 16–40 ms / 5 s | Kept (crash safety) |
| Master summary | ~14 ms | Only on change |
| Status bundle without per-category stats | < 1 ms | Kept |

Memory: `RobotsCache` holds up to `MAX_HOSTS` parsed robots.txt files for 1 h.
Measured with `tracemalloc`: 0.9 KB (WordPress-style) to 21.8 KB (150 rules)
each, so 20,000 hosts can hold ~20–400 MB. Expired entries are only evicted by
LRU. Each company site is crawled once, so a large cache buys almost nothing.

## What did you change?

- Sections 1, 3 and 4 (CPU).
- `ROBOTS_CACHE_HOSTS`: 2,000 on a small host (≈2–44 MB worst case), 20,000
  elsewhere (unchanged).
- Not changed after measurement: crawl thread count (32 per `render.yaml`).
  Peak working set in the benchmark was 118–133 MB for the measured process at
  100–1,000 URLs. orjson and httptools were already used.

## Which files changed?

- `app/backend/config.py`, `app/backend/collector/crawler.py`

## Which functions/classes changed?

- `config.ROBOTS_CACHE_HOSTS`, `crawler.RobotsCache.MAX_HOSTS`

## Why does this improve performance?

CPU goes to useful work (parsing pages) instead of re-parsing certificates
and re-writing unchanged files. Memory stays bounded on long-lived instances.

## Before / After

See the table above and the benchmarks in section 10.

## Expected impact

Large CPU reduction per crawled site (measured). Bounded robots.txt memory.

## Trade-offs / risks

A smaller robots cache means a directory site revisited after more than 2,000
other hosts is probed again (one small request).

---

# 9. Render Deployment

## What was the problem?

Verified against the repository:

| Check | Result |
|---|---|
| Bind address | `python -m backend.serve --host 0.0.0.0 --port $PORT` ✔ |
| `$PORT` | used by `startCommand` ✔ |
| Build command | `pip install -r requirements.txt` with `rootDir: app` ✔ (pinned versions) |
| Health check | `healthCheckPath: /api/health`: in memory, unauthenticated, no worker RPC ✔ |
| Secrets | `SERPER_API_KEY`, `APP_AUTH_TOKEN` are `sync: false` (entered in the dashboard). None in source; the diff was scanned ✔ |
| Proxy | `FORWARDED_ALLOW_IPS="*"`; Vercel rewrites `/api/*` to Render, so the browser sees one origin and needs no CORS config ✔ |
| Small-host sizing | `MAX_ACTIVE_JOBS=1`, 32 crawl threads, no parser processes (auto: `SMALL_HOST` from cgroup limits) ✔ |
| Graceful shutdown | uvicorn lifespan → `hub.stop()` → worker shutdown RPC; SIGTERM hardening added (section 5) |
| Restart safety | atomic checkpoint, worker supervision, single instance by design ✔ |

## What did you change?

- `render.yaml` declares `EXPORT_MIN_INTERVAL=60`, `STATUS_PUBLISH_INTERVAL=1`
  and `ROBOTS_CACHE_HOSTS=2000`. These are the same values the automatic
  small-host detection picks; declaring them keeps them in force if cgroup
  detection ever fails.
- `app/.env.example` documents the five new variables.

## Which files changed?

- `render.yaml`, `app/.env.example`

## Which functions/classes changed?

None (configuration only).

## Why does this improve performance?

It makes the small-host behavior explicit and reproducible.

## Before / After

Same start/build commands; three more explicit settings.

## Expected impact

None beyond sections 3, 4 and 8. This is about predictability.

## Trade-offs / risks

**Infrastructure limitations that code cannot remove:**

- **0.1 CPU:** the API, the worker and all parsing share one tenth of a core.
  Code reduces the work per site; it cannot add CPU.
- **512 MB RAM:** two Python processes plus state; large checkpoints grow
  memory linearly with stored records.
- **Ephemeral disk:** on Render Free the filesystem is wiped on every deploy,
  restart and spin-down. The checkpoint and Excel files are lost, so jobs
  **cannot resume across restarts** there. The browser keeps copies of Excel files
  (existing feature).
- **Sleep / cold starts:** a free instance spins down after ~15 min without
  inbound requests. A running job is killed if every browser tab is closed for
  that long. Waking up takes tens of seconds.
- **Single instance:** jobs live in the worker process, so there is no horizontal
  scaling.
- **Shutdown grace:** the clean shutdown must finish inside Render's grace
  period. At 0.1 CPU the final Excel export of a large category takes seconds.
- **External latency:** geographic distance to target sites and Serper,
  website response times, Serper latency and rate limits, and the extra hop
  through Vercel's rewrite proxy.

---

# 10. Testing / Benchmarking

## What was the problem?

The repository had unit/integration tests and an API load test with simulated
websites (`SimFetcher`), but no benchmark that runs the **real** `Fetcher`
(TLS, robots.txt, HTTP status handling, retries) under a realistic CPU limit.

## What did you change?

- `app/tests/test_performance.py`: unit tests for the shared TLS context
  (pool wiring, `verify=False` / custom CA untouched), change-only Excel exports
  (skip, force, external delete, title change, summary), and the per-category
  stats cache.
- `app/tests/loadtest/bench_crawl.py`: crawl-path benchmark.
  - It starts a separate, uncapped process that serves `firmN.co.uk` over HTTPS
    under a throw-away CA (openssl CLI), with deterministic per-host behavior:
    67 % normal (60 % rich homepage, else a `/contact` page), 4 % slow (5–9 s),
    6 % 403, 5 % 404, 4 % 429, 5 % 503. robots.txt: 70 % missing, 25 %
    partial, 5 % disallow-all. Dead hosts (6 %) resolve to a black-hole IP and
    time out; NXDOMAIN hosts (3 %) do not resolve. 12 % of the URL list are
    duplicates.
  - The measured process is capped with a **Windows Job Object CPU hard cap**
    (0.1 core ≈ Render Free), loads a copy of the real 13 MB checkpoint, uses
    the `render.yaml` settings, and runs `CollectionJob`'s real crawl path:
    claim/dedup → `crawl_site` → `Fetcher` → analysis → validate → dedup registry
    → state. It also runs the checkpoint saver, Excel exports, a status
    publisher at the configured cadence, and the final verified export.
  - `--root` runs the same benchmark against a saved copy of older code (a git
    worktree of the previous commit), so before and after use identical
    inputs.
- Raw results: `app/tests/loadtest/results/crawl_bench_2026-10-01.json`.

## Which files changed?

- `app/tests/test_performance.py` (new)
- `app/tests/loadtest/bench_crawl.py` (new)
- `app/tests/loadtest/results/crawl_bench_2026-10-01.json` (new)

## Which functions/classes changed?

Test code only.

## Why does this improve performance?

It makes regressions measurable, including correctness effects such as false
"dead site" verdicts.

## Results

Environment: Windows 11, 12 logical cores, 16 GB RAM, Python 3.12.10,
BENCH_RESULTS_ENV_PLACEHOLDER. "Before" = commit `12a9aa8` (HEAD before
this change); "After" = this change. Same URL lists, same simulated web, same
starting checkpoint.

### Capped at 0.1 CPU (Render Free emulation)

BENCH_CAPPED_TABLE_PLACEHOLDER

### Uncapped (CPU-seconds consumed)

BENCH_UNCAPPED_TABLE_PLACEHOLDER

### Live TLS (real public sites)

See section 1: 832–850 → 18–26 ms CPU per site.

### Other tests

| Test | Before | After |
|---|---|---|
| `tests/test_pipeline.py` | pass | pass |
| `tests/test_security.py` | pass | pass |
| `tests/test_credits.py` | pass | pass |
| `tests/test_concurrency.py` (concurrent jobs/users) | pass | pass |
| `tests/test_resilience.py` (failures, retries, watchdog) | pass | pass |
| `tests/test_performance.py` (new) | n/a | pass |
| `tests/loadtest/smoke.py` (API, queueing, isolation, ETag, worker crash → restart → resume) | — | pass (restart 1.6 s) |
| `tests/loadtest/shutdown_test.py` (graceful stop under load) | — | pass (228 records saved, no orphans) |
| Excel output equivalence before/after (11 workbooks) | — | identical |
| badssl.com certificate validation | — | expired / wrong host / self-signed / untrusted root all rejected |

## Before / After

See the tables above.

## Expected impact

n/a (measurement).

## Trade-offs / risks / limits of these measurements

- The CPU cap is a Windows Job Object hard cap. It approximates, but is not
  identical to, Render's Linux cgroup quota. A calibration loop measured ~0.10–0.13
  of a core.
- TLS costs depend on the OpenSSL build. These numbers are from Windows, and
  Render's Linux build was **not measured**.
- The simulated web is on loopback, so there is no real network latency for
  handshakes. Page latency is simulated (median 150 ms).
- **Not performed:** a benchmark on Render itself; Linux SIGTERM delivery;
  multi-hour soak at 0.1 CPU; live Serper calls (no credits were spent). Serper
  behavior is covered by `test_credits.py` and the simulated-Serper smoke test.
- One capped run was invalidated because the laptop went to sleep mid-run (wall
  time 15 h). It was discarded and re-run; the benchmark now holds a
  keep-awake request while it runs.

---

# Code Change Map

**File:** `app/backend/collector/tls.py` (new)
- **Purpose:** one verified TLS context shared by all HTTPS connection pools.
- **Why it was changed:** urllib3 re-parsed the CA bundle on every connection (~320 ms CPU).
- **Functions/classes modified:** `shared_tls_context()`, `SharedTLSAdapter.build_connection_pool_key_attributes()`, `SharedTLSAdapter.cert_verify()`
- **Optimization section:** 1 HTTP / SSL, 2 Crawler, 8 CPU
- **Old behavior:** fresh `SSLContext` + CA bundle parse per TLS connection.
- **New behavior:** one pre-loaded context; identical verification (`CERT_REQUIRED` + hostname).
- **Performance reason:** CA parsing moved from per-connection to per-process.
- **Potential side effects:** depends on two `requests` adapter hooks; custom CA bundles and `verify=False` fall back to stock behavior.

**File:** `app/backend/collector/crawler.py`
- **Purpose:** HTTP session, robots.txt cache, page fetching, link discovery.
- **Why it was changed:** use the shared TLS adapter; bound the robots cache on small hosts.
- **Functions/classes modified:** `make_session()`, `RobotsCache.MAX_HOSTS`
- **Optimization section:** 1, 2, 8
- **Old behavior:** stock `HTTPAdapter`; robots cache up to 20,000 hosts.
- **New behavior:** `SharedTLSAdapter` (same retries/pool sizes); cache size from `ROBOTS_CACHE_HOSTS`.
- **Performance reason:** CPU per site; bounded memory.
- **Potential side effects:** none functional; more robots.txt re-probes for rarely revisited hosts on small hosts.

**File:** `app/backend/collector/search.py`
- **Purpose:** Serper client.
- **Why it was changed:** same TLS fix for the Serper session.
- **Functions/classes modified:** `_serper_session()`
- **Optimization section:** 1, 6
- **Old behavior:** stock `HTTPAdapter`.
- **New behavior:** `SharedTLSAdapter`, same pool size.
- **Performance reason:** cheaper (re)connections.
- **Potential side effects:** none.

**File:** `app/backend/collector/exporter.py`
- **Purpose:** Excel files (per category + master summary).
- **Why it was changed:** stop rewriting unchanged workbooks.
- **Functions/classes modified:** `_save` → `_save_locked`; new `_written`, `_digest()`, `_write_if_changed()`; `write_category_file(force=)`, `write_master_summary()`
- **Optimization section:** 3, 7, 8
- **Old behavior:** every call rebuilt and replaced the file.
- **New behavior:** rebuilt only when the rows differ from what this process last wrote (or the file changed or disappeared, or `force`).
- **Performance reason:** ~0.5 s CPU per skipped 2,000-row export; fewer browser re-downloads.
- **Potential side effects:** after a restart, the first export of each file is always written (no persisted digest).

**File:** `app/backend/collector/engine.py`
- **Purpose:** state store, checkpointing, collection job.
- **Why it was changed:** per-category stats cache; forced re-export on failed verification.
- **Functions/classes modified:** `StateStore.__init__`, `StateStore.category_stats()`, `StateStore.export_categories(force=)`, `CollectionJob._finalize_outputs()`
- **Optimization section:** 3, 4, 8
- **Old behavior:** any record change ⇒ full rescan of all categories; verification retry re-exported normally.
- **New behavior:** only changed categories are recounted; verification retry forces a rewrite.
- **Performance reason:** less worker CPU per status bundle.
- **Potential side effects:** correctness relies on `touch(category)` after record changes (already true for every code path).

**File:** `app/backend/config.py`
- **Purpose:** settings and env parsing.
- **Why it was changed:** new tunables with small-host defaults; `_float_env` moved above its first use.
- **Functions/classes modified:** `_float_env` (moved), `EXPORT_MIN_INTERVAL`, `STATUS_PUBLISH_INTERVAL`, `POLL_MS_ACTIVE`, `POLL_MS_IDLE`, `ROBOTS_CACHE_HOSTS`
- **Optimization section:** 3, 4, 7, 8, 9
- **Old behavior:** constants 30 s / 0.5 s / 3 s / 10 s / 20,000.
- **New behavior:** same defaults on normal hosts; 60 s / 1 s / 2,000 on small hosts; all overridable and clamped.
- **Performance reason:** fit 0.1 CPU / 512 MB.
- **Potential side effects:** slightly staler mid-run Excel files and status on small hosts.

**File:** `app/backend/worker.py`
- **Purpose:** collection worker process.
- **Why it was changed:** survive group-wide SIGTERM until the API stops it cleanly.
- **Functions/classes modified:** `worker_main()`
- **Optimization section:** 5, 9
- **Old behavior:** SIGTERM killed the worker immediately (POSIX).
- **New behavior:** SIGTERM ignored; shutdown via the API's RPC or a broken pipe.
- **Performance reason:** reliability, not speed.
- **Potential side effects:** a worker whose API is hung but alive ignores SIGTERM (SIGKILL still works).

**File:** `app/backend/hub.py`
- **Purpose:** API-side worker supervision.
- **Why it was changed:** `terminate()` sends SIGTERM, which the worker now ignores.
- **Functions/classes modified:** `WorkerHub.stop()`
- **Optimization section:** 5
- **Old behavior:** `terminate()` after the 30 s grace period.
- **New behavior:** `kill()` after the 30 s grace period.
- **Performance reason:** n/a.
- **Potential side effects:** none (same outcome on Windows).

**File:** `render.yaml`, `app/.env.example`
- **Purpose:** deployment and configuration documentation.
- **Why it was changed:** declare and document the new tunables.
- **Functions/classes modified:** none.
- **Optimization section:** 9
- **Old behavior / New behavior:** three more explicit env values / five documented variables.
- **Performance reason:** predictable small-host behavior.
- **Potential side effects:** none.

**Files:** `app/tests/test_performance.py`, `app/tests/loadtest/bench_crawl.py`, `app/tests/loadtest/results/crawl_bench_2026-10-01.json` (new)
- **Purpose:** regression tests, benchmark, raw results.
- **Optimization section:** 10

---

# Before / After Architecture

## Before

```
Browser (Vercel page, polls /api/status every 3-10 s)
   |  /api/*  (Vercel rewrite)
FastAPI process  (fast-path GETs from memory)
   |  pipe: RPC + status bundle every 0.5 s
Worker process
   |-- status publisher: full stats rescan of ALL categories on every record change
   |-- Job controller --> Serper search pool --> Serper (keep-alive)
   |-- Crawl pool (32 threads)
   |      |-- per NEW TLS connection: new SSLContext + parse 240 KB CA bundle (~0.3 s CPU)
   |      |-- robots.txt probe (4 s / 8 s timeout)  --timeout under CPU starvation--> "dead" host, skipped
   |      '-- fetch -> parse/extract -> validate -> dedup -> state
   '-- Saver thread: checkpoint every 5 s; Excel rewritten every 30 s even if unchanged
                                   |
                         every rewrite -> browsers re-download the workbook
```

## After

```
Browser (Vercel page, polls at server-suggested cadence; configurable)
   |  /api/*  (Vercel rewrite)
FastAPI process  (fast-path GETs from memory; handles SIGTERM, stops worker cleanly)
   |  pipe: RPC + status bundle (1 s on small hosts)
Worker process  (ignores SIGTERM; shut down by the API or a broken pipe)
   |-- status publisher: recounts only the category that changed
   |-- Job controller --> Serper search pool --> Serper (keep-alive, shared TLS context)
   |-- Crawl pool (32 threads)
   |      |-- ONE shared, pre-verified TLS context (CA bundle parsed once per process)
   |      |-- robots.txt probe answers in time -> real dead hosts only (cache bounded)
   |      '-- fetch -> parse/extract -> validate -> dedup -> state
   '-- Saver thread: checkpoint every 5 s; Excel written only when content changed
                     (at most every 60 s on small hosts; final export always, verified)
```

**Component purposes**

- **Browser page:** UI; polls status at the server's suggested cadence; keeps Excel copies in IndexedDB.
- **FastAPI process:** answers reads from memory, validates writes, forwards commands to the worker. It never runs scraping.
- **Hub (in the API process):** supervises the worker, restarts it after a crash, and resubmits running jobs.
- **Worker process:** runs all jobs, so heavy work never blocks API responses.
- **Job controller:** plans searches within the credit budget, watches progress, recovers from failures.
- **Crawl pool + Fetcher + shared TLS context:** bounded concurrent site fetching with robots.txt, SSRF guard and retry rules.
- **State store + saver thread:** in-memory records and dedup registry; atomic checkpoint; change-only Excel exports.

---

# Decision Log

**1. Shared TLS context (`SharedTLSAdapter`)**
- **Alternatives considered:** pin an older `requests` (2.32.0–2.32.3 preloaded a context, later reverted upstream); add the `truststore` package (OS trust store); rewrite on httpx/aiohttp; disable verification.
- **Why selected:** ~40 lines, no new dependency, verification semantics unchanged, falls back to stock behavior for custom CA / `verify=False`.
- **Why not the others:** pinning old versions blocks security updates; `truststore` changes which CAs are trusted (behavior change, new dependency); a client rewrite is far larger and riskier; disabling verification is insecure (rejected outright).
- **Paid service:** no. **Infrastructure change:** no. **Behavior change:** none intended. Measured result: more sites crawled on small hosts, because sites are no longer misjudged as dead.

**2. Change-only Excel exports (content digest)**
- **Alternatives considered:** openpyxl `write_only` mode; generate Excel on download only; write CSV; track exports by revision counters.
- **Why selected:** exact (compares the rows actually written), self-contained in `exporter.py`, catches external edits/deletes via stat, keeps format and the `/api/files` semantics.
- **Why not the others:** `write_only` measured only 7 % faster and styles differed; on-demand generation would move ~0.5 s CPU into the download path and change file mtimes the browser relies on; CSV changes the output format; revision counters couple every mutation path to the exporter.
- **Paid service:** no. **Infrastructure change:** no. **Behavior change:** mid-run files on small hosts refresh every 60 s instead of 30 s (configurable); final files identical.

**3. Per-category stats cache + 1 s status cadence on small hosts**
- **Alternatives considered:** incrementally maintained counters on every append/delete; leave as is.
- **Why selected:** keyed on the existing `cat_rev`, so no new mutation bookkeeping.
- **Why not the others:** incremental counters would need updates in every mutation path (append, delete, reset, load), with more risk of drift.
- **Paid service:** no. **Infrastructure change:** no. **Behavior change:** none (status lag ≤ 1 s, polls every 3 s).

**4. Bounded robots.txt cache on small hosts**
- **Alternatives considered:** purge expired entries periodically; shorter TTL.
- **Why selected:** one setting, LRU already implemented.
- **Why not the others:** a purge thread adds complexity for the same bound.
- **Paid service:** no. **Infrastructure change:** no. **Behavior change:** no.

**5. Worker ignores SIGTERM; hub escalates with `kill()`**
- **Alternatives considered:** a SIGTERM handler in the worker that runs its own shutdown.
- **Why selected:** matches the existing SIGINT/SIGBREAK design. One owner (the API) drives shutdown, so there is no race between two shutdown paths.
- **Why not the others:** a worker-side handler would race the API's shutdown RPC and the supervisor's restart logic.
- **Paid service:** no. **Infrastructure change:** no. **Behavior change:** no (graceful stop becomes reliable).

**6. Not done (deliberately)**
- **No database, Redis, or queue service:** single instance by design; the file checkpoint covers recovery; Render Free has no persistent disk anyway.
- **Crawl thread count unchanged (32):** after the TLS fix, CPU per site is ~20 ms and peak memory stayed at 118–133 MB in the benchmark.
- **Checkpoint interval unchanged (5 s):** 16–40 ms per save, and it bounds data loss.
- **429 not retried:** deliberate in the original design (see section 2).
- **No async rewrite:** large change for an I/O pattern the thread pool already handles.

---

# Infrastructure Impact

## Code-only changes

Every change in this document is code or configuration. None needs a paid
service, a new dependency, or a different instance type. The application runs
on Render Free as before.

## Optional infrastructure improvements

**Render paid instance (more CPU and RAM)**
- **Why it may help:** parsing, Excel export and checkpointing are CPU-bound; at 0.1 CPU they take ~10× the wall-clock time of one core.
- **What problem it solves:** throughput per minute; shorter final export; more headroom for concurrent jobs (`MAX_ACTIVE_JOBS`).
- **Can the current code run without it:** yes.
- **Required or optional:** optional.

**Render persistent disk (paid plans)** mounted at `APP_OUTPUT_DIR`
- **Why it may help:** the checkpoint and Excel files survive deploys, restarts and spin-downs.
- **What problem it solves:** resume across restarts; no repeated Serper spend after a restart (the search cache and executed-query ledger are in the checkpoint).
- **Can the current code run without it:** yes (it already supports `APP_OUTPUT_DIR`).
- **Required or optional:** optional, but the only way to get real restart recovery on Render.

**Different Render region**
- **Why it may help:** lower RTT to target websites (TLS handshakes and page fetches are RTT-bound) and to Serper.
- **What problem it solves:** network latency per page.
- **Can the current code run without it:** yes.
- **Required or optional:** optional. Pick the region closest to the markets being collected.

**Always-on instance (paid)**
- **Why it may help:** no spin-down after 15 min idle, no cold starts.
- **What problem it solves:** jobs killed when all tabs are closed; slow first page load.
- **Can the current code run without it:** yes.
- **Required or optional:** optional.

**PostgreSQL**
- **Why it may help:** durable shared storage if the app ever runs as multiple instances.
- **What problem it solves:** state shared across instances.
- **Can the current code run without it:** yes (single instance, file checkpoint).
- **Required or optional:** not needed today.

**Redis / separate worker service**
- **Why it may help:** a job queue shared by separate API and worker services.
- **What problem it solves:** independent scaling of API and workers.
- **Can the current code run without it:** yes. API/worker separation already exists as two processes in one instance. Separate Render services cannot share a disk, so this would also need a database.
- **Required or optional:** not needed today.

---

# Final Implementation Report

1. **Files created:** `app/backend/collector/tls.py`, `app/tests/test_performance.py`, `app/tests/loadtest/bench_crawl.py`, `app/tests/loadtest/results/crawl_bench_2026-10-01.json`, `app/PERFORMANCE_OPTIMIZATION_CHANGELOG.md`.
2. **Files modified:** `app/backend/collector/crawler.py`, `app/backend/collector/search.py`, `app/backend/collector/exporter.py`, `app/backend/collector/engine.py`, `app/backend/config.py`, `app/backend/worker.py`, `app/backend/hub.py`, `render.yaml`, `app/.env.example`.
3. **Files deleted:** none.
4. **Functions/classes modified:**
   - new `shared_tls_context`, `SharedTLSAdapter`
   - `make_session`, `RobotsCache.MAX_HOSTS`, `_serper_session`
   - `_save` → `_save_locked`; new `_digest`, `_write_if_changed`; `write_category_file`, `write_master_summary`
   - `StateStore.__init__`, `StateStore.category_stats`, `StateStore.export_categories`, `CollectionJob._finalize_outputs`
   - `worker_main`, `WorkerHub.stop`
   - config constants (`EXPORT_MIN_INTERVAL`, `STATUS_PUBLISH_INTERVAL`, `POLL_MS_ACTIVE`, `POLL_MS_IDLE`, `ROBOTS_CACHE_HOSTS`)
5. **Main bottlenecks found:**
   - (a) per-connection CA bundle parsing, ~320 ms CPU, which on a 0.1-CPU host caused live sites to be misclassified as dead;
   - (b) unconditional Excel rewrites, ~0.5 s CPU each, plus browser re-downloads;
   - (c) full stats rescans in the status bundle;
   - (d) a robots cache sized for big hosts;
   - (e) SIGTERM shutdown gap.
6. **Optimizations implemented:** sections 1–5, 7–9 above (section 6: Serper verified, TLS adapter only).
7. **Before/after behavior:** API routes, request/response formats, UI, scraped fields and Excel format unchanged (routes compared; 170,125 Excel cells compared). Small-host defaults: Excel mid-run refresh 60 s, status cadence 1 s, robots cache 2,000.
8. **Benchmark results:** section 10 and the results JSON. Headline at 0.1 CPU, 500 URLs: records found 3 → 319; per-site CPU on real HTTPS sites 832–850 → 18–26 ms.
9. **CPU impact:** measured large reduction per crawled site (TLS); Excel and status savings follow from the measured per-operation costs.
10. **RAM impact:** peak working set of the measured process 118–133 MB in all benchmark runs (no regression); robots cache bounded to ~2–44 MB worst case on small hosts (was up to ~20–400 MB).
11. **Network/latency impact:** fetch p95 at 0.1 CPU, 500 URLs: 36.2 s → 5.3 s (5.3 s is the deliberately slow 4 % of simulated sites). Fewer workbook re-downloads by browsers. No change to Serper traffic.
12. **Remaining bottlenecks:**
    - HTML parsing (~20–30 ms CPU per page) is now the main CPU cost;
    - full 13 MB checkpoint rewrite every 5 s;
    - openpyxl serialization when files do change;
    - external latency.
13. **Render limitations:** see section 9 (CPU, RAM, ephemeral disk, spin-down, single instance, shutdown grace, latency).
14. **Optional infrastructure improvements:** see Infrastructure Impact (paid instance, persistent disk, region, always-on). Postgres/Redis not needed.
15. **Tests performed:**
    - all 5 existing suites (before and after)
    - new `test_performance.py`
    - API smoke test with worker crash/restart/resume
    - graceful shutdown test
    - Excel equivalence
    - badssl validation
    - live TLS benchmark
    - crawl benchmark at 100/500/1,000 URLs (capped and uncapped), with dead, NXDOMAIN, slow, 403, 404, 429 and 503 hosts and duplicate URLs/domains
16. **Tests not performed:**
    - on Render itself
    - Linux SIGTERM path
    - Linux OpenSSL cost
    - multi-hour soak at 0.1 CPU
    - live Serper (no credits spent)
    - concurrent-user load test at 0.1 CPU (the existing `test_concurrency.py` passes uncapped)
17. **Risks / follow-up:**
    - re-verify `SharedTLSAdapter` after `requests` upgrades (covered by `test_performance.py`);
    - measure on a real Render instance (`bench_crawl.py` needs Windows only for the CPU cap; on Linux use cgroup/`taskset`);
    - consider a persistent disk if restart recovery on Render matters;
    - the robots-probe timeout heuristic still misjudges sites if the CPU is saturated by something else.

---

# Addendum: configurable timeouts and `/health`

* **Timeouts are env-configurable** (`config.py`, `crawler.py`, `.env.example`), defaults unchanged:
  `FETCH_CONNECT_TIMEOUT`=5, `FETCH_READ_TIMEOUT`=12, `FETCH_DEADLINE`=25,
  `ROBOTS_CONNECT_TIMEOUT`=4, `ROBOTS_READ_TIMEOUT`=8 (was a hardcoded `(4, 8)` in
  `RobotsCache.allowed`), `SERPER_CONNECT_TIMEOUT`=5, `SERPER_READ_TIMEOUT`=30. Values are clamped.
  Defaults were deliberately not lowered for small hosts: a robots.txt timeout marks a host dead,
  and at 0.1 CPU a starved TLS handshake would then skip live sites (section 2).
* **`GET /health`** (`main.py`): same fast-path handler as `/api/health`. It sits outside `/api`, so it
  needs no auth and isn't rate-limited, like `/`. `/api/health` and `render.yaml` are unchanged.
* Tests: `test_performance.py` gained `test_timeouts_from_env` (defaults, overrides, clamping, in a
  subprocess) and `test_health_alias`. All six suites pass; a live server on `0.0.0.0:$PORT` answers
  both health routes in ~20 ms.

---

# Round 2 (2026-10-01): measured Render-Free profile and fixes

Per-change detail (problem, file, function, before/after, status) and the
rejected experiments are in [`PERFORMANCE_CHANGES.txt`](PERFORMANCE_CHANGES.txt).

## How it was measured

`tests/loadtest/bench_render_free.py` (Windows) runs one real 100-record
collection (Advisory / USA / "management consulting", fresh state) in a
process held to `BENCH_CPUS` CPU and 512 MB by Windows Job Object hard caps,
with the `render.yaml` env overrides. It reports stage times, crawl-queue
wait, CPU per stage, peak RAM, failures and connection counts. Serper
responses are recorded and replayed with their real latency, so before and
after runs see identical searches. Before and after runs were interleaved.

Calibration: the original code at 1 CPU took **35.1 s**, the same as on
Render, so 1 CPU = Render Free as observed; 0.1 CPU = the advertised limit.

## Bottlenecks found (original code)

| # | Bottleneck | Evidence |
|---|---|---|
| 1 | 32 crawl threads parsing HTML at once | 57–64% of all CPU; 95 ms CPU/page vs 41 ms one at a time (122 vs 74 ms at 0.1 CPU) |
| 2 | FIFO crawl queue on a saturated pool | Places listings ~70% yield waited behind directory links ~10%; ~240–275 crawl tasks per 100 records; tasks queued 12 s (1 CPU) / 107 s (0.1 CPU) |
| 3 | CA bundle loaded inside the first job | 0.33 s CPU at 1 CPU, 3.6 s wall at 0.1 CPU |

Measured and found **not** to be bottlenecks: Excel/state writes (2 Excel
writes, 0.65 s per run), status publishing (2.6 ms CPU per bundle), frontend
polling (already server-paced with backoff), Serper 429s/timeouts (none),
memory (peak 106–168 MB of 512).

## Changes

| Change | Files | What |
|---|---|---|
| Analysis lane | `config.py` (`ANALYZE_THREADS`), `collector/analysis.py` (`analyze`) | In-thread page analysis takes turns (1 at a time on Render); downloads stay parallel |
| Priority crawl queue | `collector/prio_pool.py` (new), `collector/engine.py` (`_submit_task`, `_submit_crawl`, `_submit_place`, `_new_crawl_pool`) | Places listings → search results → mined links; FIFO within; queued work cancelled at target |
| TLS warm-up | `worker.py` (`worker_main`) | Shared TLS context built at worker start, off the first job |
| Benchmark + tests | `tests/loadtest/bench_render_free.py` (new), `tests/test_performance.py`, `.env.example` | Repeatable Render-Free benchmark; tests for priority order and the lane |

Rejected after measuring: stripping `<script>`/`<style>` before parsing
(slower, changed names), capping page bytes (changed extracted data), 48
crawl threads (slower), larger GIL switch interval (no gain), parallel
robots.txt + homepage (adds a TLS handshake per site on a CPU-bound host).

## Results (same 100-record test, 100/100 records every run)

| Condition | Before | After | Improvement | After records/min |
|---|---|---|---|---|
| 0.1 CPU (strict Render Free) | 94.4 s | 71.8 s | 23.9% | 83.6 |
| 1 CPU, final batch (mean of 3) | 13.4 s | 11.1 s | 17.0% | 541 |
| 1 CPU, CPU-bound batch (mean of 3, changes 1+2) | 37.3 s | 26.4 s | 29.2% | 227 |

Absolute times differ between batches because this PC's background load
changed during the session; only same-batch runs are compared. Not yet
measured on Render itself.

## Remaining limitations

- Serper takes 3–4 s per search, which sets the time to the first record.
- Per-site network CPU (TLS, reads, DNS) is now the largest CPU cost.
- At a true 0.1 CPU the run is fully CPU-bound; only more CPU helps further.
- No paid service, new dependency or `render.yaml` change was added.
