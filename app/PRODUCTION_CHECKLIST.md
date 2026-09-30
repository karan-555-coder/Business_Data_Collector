# Production checklist

Run through this before every deployment. Commands run from `app/`.

## Secrets
- [ ] `app/.env` exists with a real `SERPER_API_KEY`; `.env` is NOT in Git
- [ ] `APP_AUTH_TOKEN` set to a strong code if the app is reachable beyond
      this machine (LAN launcher or reverse proxy)
- [ ] No key in source: `findstr /s /i "SERPER_API_KEY=" backend frontend`
      shows only `os.environ` lookups, no literal keys
- [ ] `serper_key.txt` (legacy, outside app/) is not deployed anywhere

## Build & dependencies
- [ ] `python -m pip install -r requirements.txt` (locked versions)
- [ ] `python -m pip_audit` → "No known vulnerabilities found"

## Tests
- [ ] `python tests\test_pipeline.py` → ALL PIPELINE TESTS PASSED
- [ ] `python tests\test_security.py` → ALL SECURITY TESTS PASSED
- [ ] `python tests\test_credits.py` → ALL CREDIT TESTS PASSED
- [ ] `python tests\test_concurrency.py` → ALL CONCURRENCY TESTS PASSED
- [ ] `python tests\loadtest\smoke.py` → SMOKE TEST PASSED
- [ ] After engine / API changes: `python tests\loadtest\run_levels.py ...`
      (README section 8) - status p99 stays in milliseconds at 2,000 users

## Runtime verification (app started)
- [ ] `GET /api/health` → status ok, expected version, `auth_required`
      matches intent, `"worker": "up"`
- [ ] Only ONE instance per data folder (no "ANOTHER INSTANCE" warning in
      `output/app.log`; no dev `--reload` server on the same folder)
- [ ] With auth on: `/api/config` without header → 401; with header → 200
- [ ] Security headers present (CSP, nosniff, frame DENY) on `/`
- [ ] `/api/download/..%2F..%2Fbackend%2Fmain.py` → 404
- [ ] `/docs`, `/openapi.json` → 404
- [ ] UI loads, categories render, a small collection (target 20) works
      end-to-end and its Excel downloads

## Exposure
- [ ] Bound to 127.0.0.1 unless LAN/proxy exposure is intended
- [ ] If internet-facing: reverse proxy with TLS, `APP_FORCE_HTTPS=1`,
      `APP_ALLOWED_ORIGINS` set, firewall allows only the proxy port
- [ ] Windows Firewall rule reviewed for python.exe (network launcher)

## Deployment safety
- [ ] Backup of the current working version + `output/` taken and dated
- [ ] `APP_VERSION` bumped in `backend/config.py`
- [ ] Rollback path tested at least once (restore backup folder, start,
      health check)

## After deploy
- [ ] `/api/health` shows the new version
- [ ] One end-to-end collection verified (or offline pipeline test if no
      credits)
- [ ] Logs clean of `unhandled error` for the first session
- [ ] Previous backup retained
