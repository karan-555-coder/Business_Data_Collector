"""LIVE credit A/B: the pre-optimization engine (bench_baseline/backend,
snapshot of 2026-09-30) vs the current one, same inputs, fresh state each,
against the REAL Serper API. COSTS CREDITS (~2 x 6 x target/4).

    python tests/bench_credits_live.py --target 100
    python tests/bench_credits_live.py --target 100 --only Recruitment

Each run's billed credits come from Serper's account balance (before/after),
so timed-out requests Serper charged are included. Results: printed table +
output/bench_credits_live.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = [  # (label, category, custom name, keywords)
    ("Recruitment / Staffing", "Recruitment", "", ""),
    ("Finance", "Finance", "", ""),
    ("Law", "Law_Firms", "", ""),
    ("Healthcare", "Medical_Healthcare", "", ""),
    ("Advisory", "Advisory", "", ""),
    ("Custom: Solar Installers", "", "Solar Installers", "solar installer"),
]


def run(root: str, label: str, case, a) -> dict:
    name, cat, custom, kws = case
    args = [sys.executable, "tests/bench_discovery.py", "--root", root, "--label", label,
            "--target", str(a.target), "--country", a.country, "--keywords", kws,
            "--max-queries", "12"]
    args += ["--custom", custom] if custom else ["--category", cat]
    out = subprocess.run(args, cwd=APP, capture_output=True, text=True, timeout=3600).stdout
    start = out.find("{")
    try:
        return json.loads(out[start:out.rfind("}") + 1])
    except ValueError:
        return {"error": out[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--country", default="USA")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    roots = [("BEFORE", os.path.join(APP, "bench_baseline")), ("AFTER", APP)]
    rows = []
    for case in CASES:
        if a.only and a.only.lower() not in case[0].lower():
            continue
        res = {lab: run(root, lab, case, a) for lab, root in roots}
        rows.append({"case": case[0], **res})
        for lab in ("BEFORE", "AFTER"):
            r = res[lab]
            if "error" in r:
                print(f"{case[0]:<26} {lab:<6} ERROR {r['error'][:200]}", flush=True)
                continue
            billed = r.get("credits_billed_balance_delta")
            cr = billed if billed is not None else r.get("credits_consumed")
            bk = r.get("by_kind", {})
            print(f"{case[0]:<26} {lab:<6} valid {r['valid_records']:>4}/{a.target}  "
                  f"credits {cr:>4}  places {bk.get('places', {}).get('requests', 0):>3}  "
                  f"organic {bk.get('organic', {}).get('requests', 0):>3}  "
                  f"dup results {r.get('duplicate_results')}  domains {r.get('unique_domains')}  "
                  f"valid/credit {round(r['valid_records'] / cr, 2) if cr else '-'}  "
                  f"{r['total_runtime_s']}s", flush=True)
    path = os.path.join(APP, "output", "bench_credits_live.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
