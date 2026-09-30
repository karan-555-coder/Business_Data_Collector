"""Graceful shutdown under a running collection (Windows Ctrl+Break):

    python tests/loadtest/shutdown_test.py [--state <demo_state.json copy>]

Starts the real server (simulated Serper + websites), runs a collection,
sends Ctrl+Break to the server's process group - the console signal every
process in it receives - and verifies: the server exits, the worker stops
the job cleanly (status "stopped" in the checkpoint), everything the UI had
reported as collected is in the on-disk checkpoint, and no process (worker,
parsers) is left behind.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import smoke  # noqa: E402
from loadgen import proc_tree  # noqa: E402

smoke.PORT = 8262


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="")
    a = ap.parse_args()
    out = tempfile.mkdtemp(prefix="shutdown_")
    cmd = [sys.executable, os.path.join(HERE, "server.py"), "--root", ROOT,
           "--port", str(smoke.PORT), "--out", out]
    if a.state:
        cmd += ["--state", a.state]
    log = open(os.path.join(out, "console.txt"), "w")
    srv = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    ok = False
    try:
        assert smoke.wait(lambda: smoke.req("GET", "/api/health")[0] == 200, 60)
        cid = "s" * 32
        s, _, job = smoke.req("POST", "/api/collect", cid, {
            "category": "Law_Firms", "country": "USA", "target": 2000})
        assert s == 200, (s, job)
        st = smoke.wait(lambda: (lambda r: r if r["job"]["collected"] >= 150 else None)(
            smoke.req("GET", "/api/status", cid)[2]), 120, 1.0)
        assert st, "collection made no progress"
        time.sleep(1.0)
        seen = smoke.req("GET", "/api/status", cid)[2]["job"]["collected"]
        tree = list(proc_tree(srv.pid))
        print(f"collecting: {seen} records reported; {len(tree)} processes "
              f"(server, worker, parsers); sending Ctrl+Break")
        t0 = time.time()
        os.kill(srv.pid, signal.CTRL_BREAK_EVENT)
        srv.wait(90)
        print(f"server exited in {time.time() - t0:.1f}s (code {srv.returncode})")
        left = smoke.wait(lambda: not any(smoke._pid_exists(p) for p in tree), 30)
        assert left, f"processes left behind: {[p for p in tree if smoke._pid_exists(p)]}"
        print("no process left behind: OK")
        with open(os.path.join(out, "demo_state.json"), "rb") as fh:
            state = json.loads(fh.read())
        on_disk = len(state["records"]["Law_Firms"])
        meta = state.get("jobs", {}).get("Law_Firms", {})
        assert on_disk >= seen, f"lost progress: UI showed {seen}, checkpoint has {on_disk}"
        assert meta.get("status") == "stopped", meta.get("status")
        print(f"checkpoint: {on_disk} records (UI had shown {seen}), job meta "
              f"status '{meta.get('status')}': OK")
        with open(os.path.join(out, "worker.log"), encoding="utf-8") as fh:
            wl = fh.read()
        assert "collection worker exiting" in wl
        print("worker logged a clean exit: OK")
        ok = True
    finally:
        if srv.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(srv.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.close()
        print("server logs in", out)
    print("\nSHUTDOWN TEST PASSED" if ok else "\nSHUTDOWN TEST FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
