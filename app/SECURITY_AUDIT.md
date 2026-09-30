# Security audit — 2026-09-29

Scope: full `app/` codebase (backend, frontend, launchers, tests).
Method: manual code review per threat class + automated checks
(`pip-audit`, `pyflakes`, `tests/test_security.py`, live API smoke test with
auth/rate-limit/traversal/oversize probes).

## Findings by threat class

| Threat | Verdict | Notes |
|---|---|---|
| SQL injection | N/A | No database; state is a JSON file written by the app only. |
| XSS | Mitigated | UI inserts all dynamic values via `textContent`; audit found no interpolation of record/log data into HTML. CSP as backstop. Website links restricted to `http(s)` + `noopener noreferrer`. |
| SSRF | **Fixed this audit** | Previously the crawler fetched any search-result URL. Now every fetch and every redirect hop is validated (schemes, ports, blocked hostnames, DNS resolution must be all-public). Unit-tested against localhost, [::1], 169.254.169.254, metadata hostnames, RFC1918, CGN, file://, gopher://, odd ports. |
| CSRF | Low risk / mitigated | No cookies or sessions — auth is a custom header, which cross-site forms cannot set; CORS restricts readable cross-origin calls. |
| IDOR / authz | N/A | Single-tenant: one shared dataset, one optional access code. No per-user objects exist. |
| Auth bypass | Mitigated | Optional `APP_AUTH_TOKEN` enforced in middleware for all `/api` except health; constant-time compare; OPTIONS exempt (CORS preflights carry no credentials and reach no handler logic). Default (unset) = open, intended for localhost only — documented. |
| Path traversal | Mitigated | Downloads resolved only via exact-match whitelist of generated filenames; probed with encoded traversal payloads → 404. No other file-serving routes. |
| Command injection | N/A | No shell invocations, no `eval`/`exec`, crawled content never executed. |
| File upload | N/A | No upload endpoints. |
| API abuse / brute force | Mitigated | Per-IP rate limits (240/min API, 10/min collect), 64 KB body cap, single-job lock, Serper spend caps, auth failures + limit violations logged with IP. |
| Info leakage | Mitigated | Global handler returns generic 500s; stack traces server-side only. API docs/OpenAPI disabled. Frontend receives `serper_key_present` boolean only. No source/static directories mounted. Excel formula injection neutralized. |
| Insecure CORS | Mitigated | Explicit origin list, never `*`; production origins via env. |
| Dependency vulns | Clean | `pip-audit` clean after upgrading `pip` itself (12 advisories were against pip 25.0.1, not app deps). Top-level deps pinned in requirements.txt. |

## Reliability review ("one failure must not kill the run")

Verified in code and exercised by tests: per-URL try/except with failure
accounting, 120 s batch timeout abandons wedged fetches, Serper 429 backoff
and 401/403/credit detection disable the provider cleanly, per-query JSON
checkpoint + atomic file replace (`.tmp` + `os.replace`), resume skips
executed queries, Excel writes are atomic and a locked-open file degrades to
a warning, category worker exceptions are caught and reported as job errors
with state saved.

## Residual risks (accepted, documented)

1. **DNS rebinding**: the SSRF check resolves the hostname before fetching;
   an attacker-controlled DNS server with ~0 TTL could theoretically answer
   public for the check and private for the fetch. Full fix requires IP
   pinning at the transport layer. Impact bounded by: 2 MB cap, GET-only,
   response parsed as HTML only, no credentials attached.
2. **No HTTPS termination in-app**: TLS must come from a reverse proxy;
   plain HTTP is acceptable only on localhost/trusted LAN.
3. **In-memory rate limits** reset on restart and are per-process — fine at
   this scale; a multi-worker deployment would need a shared store.
4. **Shared access code** is a single token, not per-user accounts — right-
   sized for a single-team tool; anyone with the code has full access.
5. **CSP allows `unsafe-inline`** because the UI is a single inline-script
   page; XSS defense rests primarily on the strict `textContent` discipline.
6. **localStorage token**: an XSS breach would expose the access code —
   mitigated by (5)'s discipline and CSP's other restrictions.

## Addendum 2026-09-30 — multi-user scalability changes

Reviewed the changes that introduced per-user jobs and the worker process:

| Area | Verdict | Notes |
|---|---|---|
| IDOR (jobs) | Mitigated | Jobs now belong to a browser's random `X-Client-Id`. Status/log/stop resolve the job from the caller's id only; `POST /api/stop` with another user's `job_id` → 404. Owner ids are stripped from every response (checked by `tests/loadtest/smoke.py`). The id is a separation token, not authentication — access control stays `APP_AUTH_TOKEN`. |
| Rate-limit bypass | Accepted | Per-user limits key on the self-asserted client id; rotating ids only moves an attacker onto the per-IP ceiling (60,000/min), which still bounds a single address. |
| Resource exhaustion | Mitigated | Bounded job slots / queue / per-user jobs, bounded thread pools (global crawl budget), bounded caches (robots LRU 20k, SSRF DNS 20k, records/download LRUs, job history 300), `HTTP_LIMIT_CONCURRENCY` sheds overload with 503s. |
| Fast-path routes | Mitigated | The hot GET routes bypass FastAPI but NOT the guard: size limit, rate limits, auth token and security headers apply exactly as before (same `Guard` code path). Query parameters are parsed defensively (bad ints → defaults, unknown categories → 400). |
| IPC | Mitigated | API ↔ worker over a private multiprocessing pipe, no listening socket. Orphaned parser processes after a worker crash were found in testing and fixed (they now exit with their parent). |

Residual risk 3 above still holds: rate limits are in-memory, per process.

## Explicitly out of scope

Docker images (not used), database hardening (no database), multi-tenant
authorization, WAF/DDoS protection (use the reverse proxy / a CDN if
internet-facing).
