# Load test results — 2026-09-30

Machine: 12 logical cores, 16 GB RAM, Windows 11, Python 3.12. Server and
load generator on the same machine. Each level ran against a fresh server
started from a copy of the real 11 MB checkpoint; simulated Serper (5 req/s
account limit, ~1.2 s latency) and simulated websites (~0.5 s, 8 % dead,
2 % slow); 15 s ramp-up, 60 s measured. 8 users start a collection
(target 2,000 each); every user polls status every 3 s — the worst case
(the real page polls every 10 s while the user has no running collection).
Raw data: `*_summary.json` in this folder.

## Before (baseline code)

| users | req/s served | status p50 | status p95 | health p99 | errors | jobs accepted | records/min |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 13 | 4,928 ms | 7,130 ms | 7,152 ms | 0 % | 1 / 8 | 158 |
| 500 | 86 | 2,031 ms | 24,927 ms | 9,576 ms | 78 % | 1 / 8 | 231 |
| 1,000 | 205 | 2,025 ms* | 3,551 ms* | 5,228 ms | 86 % | 0 / 8 | 0 |
| 2,000 | 428 | 2,030 ms* | 2,049 ms* | 15,047 ms | 95 % | 0 / 8 | 0 |

\* most requests failed fast (connection refused / reset); latencies of
the few that succeeded.

## After (final code)

| users | req/s served | status p50 | status p95 | status p99 | health p99 | errors | jobs accepted | records/min (4 running) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 36 | 0.9 ms | 2.2 ms | 3.4 ms | 3.1 ms | 0 % | 8 / 8 | 1,818 |
| 500 | 179 | 1.2 ms | 3.0 ms | 8.2 ms | 7.4 ms | 0 % | 8 / 8 | 1,880 |
| 1,000 | 360 | 1.6 ms | 4.1 ms | 9.2 ms | 13.9 ms | 0 % | 8 / 8 | 1,922 |
| 2,000 | 720 | 2.4 ms | 6.0 ms | 14.5 ms | 16.3 ms | 0 % | 8 / 8 | 1,906 |
| 3,000 | 1,082 | 3.2 ms | 9.3 ms | 18.3 ms | 17.7 ms | 0 % | 8 / 8 | 1,824 |

Whole process tree (API + worker + 8 parsers) at 2,000 users: ~68 % of
one core on average, 673 MB working set, 316 threads.

Soak, 2,000 users for 10 minutes (~400,000 status polls): p50 2.7 ms,
p95 8.3 ms, p99 21.4 ms, 0 errors; the first 4 collections reached their
2,000 targets and the 4 queued ones started automatically; memory grew
675 → 716 MB while ~9,000 records were added (data growth, no leak trend).

## Iterations

1. **Round 1** (worker process, per-user jobs, caches, orjson, pure-ASGI
   guard): fine to 1,000 users (p99 36 ms) but at 2,000 the API saturated
   one core at ~650 req/s (p95 2.5 s, 0.14 % errors). Profiling showed
   FastAPI's routing / dependency machinery costing ~1.2 ms per request.
2. **Round 2** (hot GET routes served by plain ASGI handlers; CORS skipped
   for same-origin): per-request app cost 1,532 → 250 µs; 2,000 users at
   p99 11.9 ms, 0 errors.
3. **Serper tuning**: soak logs showed 4 jobs using only ~2.4 of the
   account's 5 req/s (the adaptive limiter overshot an 8 req/s ceiling and
   halved on every 429). Ceiling now follows the account's advertised
   rateLimit, gentler back-off: collection throughput ~990 → ~1,900
   records/min with 4 jobs, 0 extra API latency.

Also verified: worker crash → restarted in ~2 s, running jobs resumed,
its parser processes exit with it (an orphan-process leak found during
testing was fixed); Ctrl+Break → job stopped cleanly, final checkpoint
written, no process left behind.
