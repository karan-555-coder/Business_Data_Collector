"""Run the load test at several user levels, each against a FRESH server
instance (same starting checkpoint), and write a combined report.

    python tests/loadtest/run_levels.py --root . --state <demo_state.json copy>
        --levels 100,500,1000,2000 --client new --jobs 8 --tag new
    python tests/loadtest/run_levels.py --root <baseline copy> --baseline
        --client old --jobs 8 --tag baseline ...
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def wait_health(port: int, timeout: float = 90.0) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                        timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def kill_tree(pid: int):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--levels", default="100,500,1000,2000")
    ap.add_argument("--client", default="new")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--job-target", type=int, default=2000)
    ap.add_argument("--hold", type=float, default=60)
    ap.add_argument("--ramp", type=float, default=15)
    ap.add_argument("--force-poll-ms", type=int, default=0)
    ap.add_argument("--port", type=int, default=8250)
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    a = ap.parse_args()

    os.makedirs(a.results, exist_ok=True)
    combined = []
    for level in [int(x) for x in a.levels.split(",")]:
        out = tempfile.mkdtemp(prefix=f"lt_{a.tag}_{level}_")
        log_path = os.path.join(out, "server_console.log")
        cmd = [sys.executable, os.path.join(HERE, "server.py"), "--root", a.root,
               "--port", str(a.port), "--out", out, "--state", a.state]
        if a.baseline:
            cmd.append("--baseline")
        with open(log_path, "w") as log:
            srv = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_health(a.port):
                print(f"[{level}] server did not come up; see {log_path}")
                continue
            report = os.path.join(a.results, f"{a.tag}_{level}.json")
            subprocess.run([sys.executable, os.path.join(HERE, "loadgen.py"),
                            "--port", str(a.port), "--users", str(level),
                            "--client", a.client, "--jobs", str(a.jobs),
                            "--job-target", str(a.job_target), "--hold", str(a.hold),
                            "--ramp", str(a.ramp), "--force-poll-ms",
                            str(a.force_poll_ms), "--pidfile",
                            os.path.join(out, "server.pid"), "--report", report],
                           check=False)
            if os.path.exists(report):
                with open(report) as fh:
                    rep = json.load(fh)
                rep.pop("timeline", None)
                combined.append(rep)
        finally:
            kill_tree(srv.pid)
            time.sleep(2)
            # keep the server logs of the run for diagnosis, drop the data
            keep = os.path.join(a.results, f"{a.tag}_{level}_logs")
            os.makedirs(keep, exist_ok=True)
            for name in ("server_console.log", "app.log", "worker.log"):
                src = os.path.join(out, name)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(keep, name))
            shutil.rmtree(out, ignore_errors=True)
    with open(os.path.join(a.results, f"{a.tag}_summary.json"), "w") as fh:
        json.dump(combined, fh, indent=1)
    print(f"\n##### {a.tag} summary #####")
    print(f"{'users':>6}{'rps':>8}{'st p50':>8}{'st p95':>8}{'st p99':>9}{'rec p95':>9}"
          f"{'health p99':>11}{'err%':>7}{'cpu%':>7}{'rssMB':>7}{'thr':>6}  jobs")
    for r in combined:
        st = r["endpoints"].get("status", {})
        rc = r["endpoints"].get("records", {})
        started = [j for j in r["jobs"] if j["start_status"] == 200]
        running = sum(1 for j in started if j["records_per_min"] > 0)
        rpm = sum(j["records_per_min"] for j in started)
        print(f"{r['users']:>6}{r['throughput_rps']:>8}{st.get('p50', 0):>8}"
              f"{st.get('p95', 0):>8}{st.get('p99', 0):>9}{rc.get('p95', 0):>9}"
              f"{r['health_probe']['p99']:>11}{100 * r['error_rate']:>7.2f}"
              f"{r['server']['cpu_pct_avg']:>7}{r['server']['rss_mb_max']:>7}"
              f"{r['server']['threads_max']:>6}  {len(started)}/{len(r['jobs'])} "
              f"accepted, {running} progressing, {rpm:.0f} rec/min")


if __name__ == "__main__":
    main()
