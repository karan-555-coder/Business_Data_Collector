"""Load generator: N simulated browser users against a running app instance.

Each user behaves like the real frontend (page load, config, status polling,
records table, file list, starting / watching a collection). Two client
models:

  old  the frontend before the scalability work: status every 3 s with the
       full 300-line log, the records table (up to 2,000 rows) re-downloaded
       every 15 s whenever no job is running.
  new  the current frontend: status polling with an incremental log cursor
       at the server-suggested cadence (or --force-poll-ms for a worst case),
       records re-fetched only when the category count changed (If-None-Match).

Users are spread over --procs worker processes (asyncio, raw HTTP/1.1
keep-alive, one connection per user, no third-party packages). The parent
samples server CPU / memory / threads (server process + its children, i.e.
the HTML-analysis pool) and merges everything into one JSON report.

    python tests/loadtest/loadgen.py --port 8250 --users 1000 --client new
        --jobs 8 --hold 60 --pidfile <out>/server.pid --report r.json
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import random
import subprocess
import sys
import time
import uuid
import zlib
from ctypes import wintypes

CATS = ["Finance", "CDS_Corporate_Compliance", "Forms", "Advisory", "Law_Firms",
        "Recruitment", "RPO", "Medical_Healthcare", "3D_Studios", "Other_B2B"]
JOB_CATS = ["Law_Firms", "RPO", "Advisory", "Finance", "Other_B2B",
            "CDS_Corporate_Compliance"]
ACTIVE = ("pending", "queued", "running", "recovering", "waiting")


# --------------------------------------------------------------------------- #
# Minimal HTTP/1.1 keep-alive client
# --------------------------------------------------------------------------- #

class Conn:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.r = self.w = None

    async def close(self):
        if self.w is not None:
            try:
                self.w.close()
            except Exception:
                pass
        self.r = self.w = None

    async def _request(self, method, path, headers, body):
        if self.w is None:
            self.r, self.w = await asyncio.open_connection(self.host, self.port,
                                                           limit=1 << 22)
        lines = [f"{method} {path} HTTP/1.1", f"Host: {self.host}:{self.port}",
                 "Accept-Encoding: gzip, deflate, br", "Connection: keep-alive",
                 "User-Agent: loadgen"]
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        self.w.write(("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b""))
        await self.w.drain()
        status_line = await self.r.readline()
        if not status_line:
            raise ConnectionError("closed")
        status = int(status_line.split()[1])
        hdrs = {}
        while True:
            line = await self.r.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            hdrs[k.strip().lower()] = v.strip()
        if method == "HEAD" or status in (204, 304):
            data = b""
        elif "content-length" in hdrs:
            data = await self.r.readexactly(int(hdrs["content-length"]))
        elif hdrs.get("transfer-encoding", "").lower() == "chunked":
            parts = []
            while True:
                size = int((await self.r.readline()).split(b";")[0], 16)
                chunk = await self.r.readexactly(size + 2)
                if size == 0:
                    break
                parts.append(chunk[:-2])
            data = b"".join(parts)
        else:
            data = await self.r.read()
            await self.close()
        if hdrs.get("connection", "").lower() == "close":
            await self.close()
        return status, hdrs, data

    async def request(self, method, path, headers=None, body=None, timeout=30.0):
        try:
            return await asyncio.wait_for(self._request(method, path, headers, body),
                                          timeout)
        except asyncio.TimeoutError:
            await self.close()
            return 0, {}, b""
        except (OSError, ConnectionError, asyncio.IncompleteReadError, ValueError,
                IndexError):
            await self.close()
            return -1, {}, b""


def decode_json(hdrs: dict, data: bytes):
    if not data:
        return None
    if hdrs.get("content-encoding") == "gzip":
        data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
    try:
        return json.loads(data)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Worker: simulated users
# --------------------------------------------------------------------------- #

class Stats:
    def __init__(self, measure_from: float):
        self.measure_from = measure_from
        self.lat: dict[str, list[float]] = {}
        self.codes: dict[str, dict[str, int]] = {}
        self.bytes: dict[str, int] = {}
        self.jobs: list[dict] = []
        self.probe: list[float] = []

    def add(self, ep: str, status: int, ms: float, nbytes: int, t: float):
        c = self.codes.setdefault(ep, {})
        c[str(status)] = c.get(str(status), 0) + 1
        if t >= self.measure_from:
            self.lat.setdefault(ep, []).append(ms)
            self.bytes[ep] = self.bytes.get(ep, 0) + nbytes


async def call(conn: Conn, st: Stats, ep: str, method: str, path: str,
               headers: dict, body: bytes | None = None, timeout=30.0):
    t = time.time()
    t0 = time.perf_counter()
    status, hdrs, data = await conn.request(method, path, headers, body, timeout)
    st.add(ep, status, (time.perf_counter() - t0) * 1000, len(data), t)
    return status, hdrs, data


async def user(i: int, a, st: Stats, t_start: float, t_end: float, job_cat: str | None):
    await asyncio.sleep(max(0.0, t_start - time.time()) + random.uniform(0, a.ramp))
    conn = Conn(a.host, a.port)
    cid = uuid.uuid4().hex
    hdr = {"X-Client-Id": cid}
    cat = random.choice(CATS)
    etags: dict[str, str] = {}
    log_seq = 0
    poll_ms = 3000
    last_rec_fetch = 0.0
    last_rec_count = None
    my_status = None
    job_info = None

    async def get_records():
        nonlocal last_rec_fetch
        path = f"/api/records?category={cat}&limit=2000"
        h = dict(hdr)
        if a.client == "new" and path in etags:
            h["If-None-Match"] = etags[path]
        status, hdrs, _ = await call(conn, st, "records", "GET", path, h, timeout=60)
        if status == 200 and "etag" in hdrs:
            etags[path] = hdrs["etag"]
        last_rec_fetch = time.time()

    # --- page load --------------------------------------------------------
    await call(conn, st, "page", "GET", "/", hdr)
    await call(conn, st, "config", "GET", "/api/config", hdr)
    await call(conn, st, "files", "GET", "/api/files", hdr)
    await get_records()

    started_job = False
    job_t0 = time.time() + random.uniform(0, 5) if job_cat else None
    while time.time() < t_end:
        # --- start a collection (job users) ---------------------------------
        if job_cat and not started_job and time.time() >= job_t0:
            started_job = True
            body = json.dumps({"category": job_cat, "keywords": "", "country": "USA",
                               "target": a.job_target, "provider": "serper",
                               "max_queries": 60}).encode()
            status, hdrs, data = await call(conn, st, "collect", "POST", "/api/collect",
                                            hdr, body)
            j = decode_json(hdrs, data) or {}
            job_info = {"category": job_cat, "start_status": status,
                        "start_detail": str(j.get("detail", ""))[:120],
                        "t_start": time.time(), "samples": []}
            st.jobs.append(job_info)
        # --- status poll -----------------------------------------------------
        path = f"/api/status?log_after={log_seq}" if a.client == "new" else "/api/status"
        status, hdrs, data = await call(conn, st, "status", "GET", path, hdr)
        prev_status = my_status
        if status == 200:
            js = decode_json(hdrs, data) or {}
            job = js.get("job")
            if a.client == "new":
                poll_ms = int(js.get("poll_ms") or 3000)
            if job:
                my_status = job.get("status")
                if a.client == "new":
                    log_seq = int(job.get("log_seq") or log_seq)
                if job_info is not None and (a.client == "new" or started_job):
                    job_info["samples"].append((round(time.time() - job_info["t_start"], 1),
                                                job.get("collected"), my_status))
            pc = (js.get("per_category") or {}).get(cat) or {}
            count = pc.get("count")
        else:
            count = None
        # terminal transition -> files + records (both clients)
        if prev_status in ACTIVE and my_status not in ACTIVE:
            await call(conn, st, "files", "GET", "/api/files", hdr)
            await get_records()
        # idle records refresh
        running = my_status in ACTIVE
        if a.client == "old":
            if not running and time.time() - last_rec_fetch >= 15:
                await get_records()
        else:
            if (not running and count is not None and count != last_rec_count
                    and time.time() - last_rec_fetch >= 15):
                last_rec_count = count
                await get_records()
        wait_ms = a.force_poll_ms or (3000 if a.client == "old" else poll_ms)
        await asyncio.sleep(wait_ms / 1000 * random.uniform(0.9, 1.1))
    if job_info is not None:
        job_info["final_status"] = my_status
    await conn.close()


async def probe(a, st: Stats, t_start: float, t_end: float):
    """UI responsiveness probe: /api/health every 250 ms on its own connection."""
    await asyncio.sleep(max(0.0, t_start - time.time()))
    conn = Conn(a.host, a.port)
    while time.time() < t_end:
        t = time.time()
        t0 = time.perf_counter()
        status, _, _ = await conn.request("GET", "/api/health", {}, None, 30)
        ms = (time.perf_counter() - t0) * 1000
        if t >= st.measure_from:
            st.probe.append(ms if status == 200 else 30000.0)
        await asyncio.sleep(0.25)
    await conn.close()


async def worker_main(a):
    t_start = a.t0
    t_measure = t_start + a.ramp + 5
    t_end = t_measure + a.hold
    st = Stats(t_measure)
    tasks = []
    for k in range(a.users):
        job_cat = None
        gidx = a.index + k * a.procs            # global user number
        if gidx < a.jobs:
            job_cat = (JOB_CATS[gidx] if gidx < len(JOB_CATS)
                       else f"__custom__:Load Test Niche {gidx}")
        tasks.append(user(gidx, a, st, t_start, t_end, job_cat))
    if a.index == 0:
        tasks.append(probe(a, st, t_start, t_end))
    await asyncio.gather(*tasks)
    with open(a.out, "w") as fh:
        json.dump({"lat": st.lat, "codes": st.codes, "bytes": st.bytes,
                   "jobs": st.jobs, "probe": st.probe}, fh)


# custom-category jobs: rewrite the payload
_orig_dumps = json.dumps


def _patched_dumps(obj, *args, **kw):
    if isinstance(obj, dict) and str(obj.get("category", "")).startswith("__custom__:"):
        obj = dict(obj, custom_category=obj["category"].split(":", 1)[1],
                   category="__custom__")
    return _orig_dumps(obj, *args, **kw)


json.dumps = _patched_dumps


# --------------------------------------------------------------------------- #
# Server resource sampling (Windows, ctypes)
# --------------------------------------------------------------------------- #

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


k32 = ctypes.windll.kernel32 if os.name == "nt" else None


def proc_tree(root: int) -> dict[int, int]:
    """pid -> thread count for root and all its descendants."""
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    rows = []
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        rows.append((e.th32ProcessID, e.th32ParentProcessID, e.cntThreads))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    out = {}
    frontier = {root}
    while frontier:
        nxt = set()
        for pid, ppid, thr in rows:
            if pid in frontier and pid not in out:
                out[pid] = thr
            if ppid in frontier and pid not in out and pid not in frontier:
                nxt.add(pid)
        frontier = nxt
    return out


def proc_usage(pid: int) -> tuple[float, int]:
    """(cpu seconds, working set bytes)."""
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not h:
        return 0.0, 0
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt),
                            ctypes.byref(ut))
        cpu = ((kt.dwHighDateTime << 32 | kt.dwLowDateTime)
               + (ut.dwHighDateTime << 32 | ut.dwLowDateTime)) / 1e7
        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        return cpu, pmc.WorkingSetSize
    finally:
        k32.CloseHandle(h)


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def summarize(parts: list[dict], samples: list[dict], a) -> dict:
    lat, codes, nbytes, jobs, probe_ms = {}, {}, {}, [], []
    for p in parts:
        for ep, xs in p["lat"].items():
            lat.setdefault(ep, []).extend(xs)
        for ep, cs in p["codes"].items():
            for c, n in cs.items():
                codes.setdefault(ep, {})[c] = codes.setdefault(ep, {}).get(c, 0) + n
        for ep, n in p["bytes"].items():
            nbytes[ep] = nbytes.get(ep, 0) + n
        jobs.extend(p["jobs"])
        probe_ms.extend(p["probe"])
    eps = {}
    total_req = total_err = 0
    for ep in sorted(set(lat) | set(codes)):
        xs = lat.get(ep, [])
        cs = codes.get(ep, {})
        n_all = sum(cs.values())
        errs = sum(n for c, n in cs.items() if c in ("0", "-1") or c.startswith("5"))
        throttled = cs.get("429", 0)
        total_req += n_all
        total_err += errs
        eps[ep] = {"n": len(xs), "rps": round(len(xs) / a.hold, 1),
                   "p50": round(pct(xs, 50), 1), "p95": round(pct(xs, 95), 1),
                   "p99": round(pct(xs, 99), 1), "max": round(max(xs), 1) if xs else 0,
                   "kb_avg": round(nbytes.get(ep, 0) / max(1, len(xs)) / 1024, 1),
                   "errors": errs, "throttled_429": throttled, "codes": cs}
    hold = [s for s in samples if s["t"] >= 0]
    cpu = [s["cpu_pct"] for s in hold]
    rss = [s["rss_mb"] for s in hold]
    thr = [s["threads"] for s in hold]
    job_rows = []
    for j in jobs:
        s = j.get("samples") or []
        first = next((c for _, c, _ in s if c is not None), None)
        last = s[-1][1] if s else None
        span = (s[-1][0] - s[0][0]) if len(s) > 1 else 0
        job_rows.append({"category": j["category"][:40], "start_status": j["start_status"],
                         "detail": j.get("start_detail", ""),
                         "final_status": j.get("final_status"),
                         "collected_first": first, "collected_last": last,
                         "records_per_min": round(60 * ((last or 0) - (first or 0)) / span, 1)
                         if span and last is not None and first is not None else 0})
    return {"users": a.users, "client": a.client, "hold_s": a.hold, "jobs": job_rows,
            "endpoints": eps,
            "total_requests": total_req, "total_errors": total_err,
            "error_rate": round(total_err / max(1, total_req), 4),
            "throughput_rps": round(sum(e["n"] for e in eps.values()) / a.hold, 1),
            "health_probe": {"n": len(probe_ms), "p50": round(pct(probe_ms, 50), 1),
                             "p95": round(pct(probe_ms, 95), 1),
                             "p99": round(pct(probe_ms, 99), 1),
                             "max": round(max(probe_ms), 1) if probe_ms else 0},
            "server": {"cpu_pct_avg": round(sum(cpu) / max(1, len(cpu)), 1),
                       "cpu_pct_max": round(max(cpu), 1) if cpu else 0,
                       "rss_mb_max": round(max(rss), 1) if rss else 0,
                       "rss_mb_end": round(rss[-1], 1) if rss else 0,
                       "threads_max": max(thr) if thr else 0,
                       "processes": hold[-1]["procs"] if hold else 0}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8250)
    ap.add_argument("--users", type=int, default=100)
    ap.add_argument("--client", choices=("old", "new"), default="new")
    ap.add_argument("--jobs", type=int, default=0, help="users that start a collection")
    ap.add_argument("--job-target", type=int, default=2000)
    ap.add_argument("--ramp", type=float, default=15.0)
    ap.add_argument("--hold", type=float, default=60.0)
    ap.add_argument("--procs", type=int, default=0)
    ap.add_argument("--force-poll-ms", type=int, default=0)
    ap.add_argument("--pidfile", default="")
    ap.add_argument("--report", default="")
    # worker mode
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--t0", type=float, default=0.0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.worker:
        asyncio.run(worker_main(a))
        return

    procs = a.procs or max(1, min(8, a.users // 250 + 1))
    t0 = time.time() + 3.0
    tmpdir = os.path.join(os.path.dirname(os.path.abspath(a.report or __file__)),
                          f"_lg_{os.getpid()}")
    os.makedirs(tmpdir, exist_ok=True)
    children = []
    for k in range(procs):
        n = a.users // procs + (1 if k < a.users % procs else 0)
        out = os.path.join(tmpdir, f"w{k}.json")
        cmd = [sys.executable, os.path.abspath(__file__), "--worker",
               "--host", a.host, "--port", str(a.port), "--users", str(n),
               "--client", a.client, "--jobs", str(a.jobs),
               "--job-target", str(a.job_target), "--ramp", str(a.ramp),
               "--hold", str(a.hold), "--procs", str(procs), "--index", str(k),
               "--t0", str(t0), "--out", out,
               "--force-poll-ms", str(a.force_poll_ms)]
        children.append((subprocess.Popen(cmd), out))

    # sample server resources until the workers finish
    pid = int(open(a.pidfile).read().strip()) if a.pidfile else 0
    samples, prev = [], None
    t_measure = t0 + a.ramp + 5
    while any(p.poll() is None for p, _ in children):
        if pid and k32 is not None:
            tree = proc_tree(pid)
            cpu = rss = 0.0
            for p_ in tree:
                c, w = proc_usage(p_)
                cpu += c
                rss += w
            now = time.time()
            if prev is not None:
                samples.append({"t": round(now - t_measure, 1),
                                "cpu_pct": round(100 * (cpu - prev[1]) / (now - prev[0]), 1),
                                "rss_mb": round(rss / 2**20, 1),
                                "threads": sum(tree.values()), "procs": len(tree)})
            prev = (now, cpu)
        time.sleep(1.0)
    parts = []
    for p, out in children:
        with open(out) as fh:
            parts.append(json.load(fh))
    rep = summarize(parts, samples, a)
    rep["cpu_note"] = ("cpu_pct = % of ONE core summed over the server process and "
                       f"its children; machine has {os.cpu_count()} logical cores")
    rep["timeline"] = samples
    if a.report:
        with open(a.report, "w") as fh:
            json.dump(rep, fh, indent=1)
    print_report(rep)


def print_report(rep: dict):
    print(f"\n=== {rep['users']} users, client={rep['client']}, hold {rep['hold_s']}s ===")
    print(f"{'endpoint':<9}{'n':>7}{'rps':>7}{'p50':>8}{'p95':>8}{'p99':>9}{'max':>9}"
          f"{'KB':>8}{'err':>6}{'429':>6}")
    for ep, e in rep["endpoints"].items():
        print(f"{ep:<9}{e['n']:>7}{e['rps']:>7}{e['p50']:>8}{e['p95']:>8}{e['p99']:>9}"
              f"{e['max']:>9}{e['kb_avg']:>8}{e['errors']:>6}{e['throttled_429']:>6}")
    hp = rep["health_probe"]
    print(f"health probe p50 {hp['p50']} p95 {hp['p95']} p99 {hp['p99']} max {hp['max']} ms")
    s = rep["server"]
    print(f"throughput {rep['throughput_rps']} req/s | errors {rep['total_errors']} "
          f"({100 * rep['error_rate']:.2f}%) | server CPU avg {s['cpu_pct_avg']}% max "
          f"{s['cpu_pct_max']}% (of one core) | RSS max {s['rss_mb_max']} MB | "
          f"threads max {s['threads_max']} | procs {s['processes']}")
    for j in rep["jobs"]:
        print(f"  job {j['category']:<28} start={j['start_status']} "
              f"final={j['final_status']} collected {j['collected_first']}->"
              f"{j['collected_last']} ({j['records_per_min']}/min) {j['detail'][:60]}")


if __name__ == "__main__":
    main()
