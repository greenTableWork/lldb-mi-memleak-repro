#!/usr/bin/env python3
"""Compare matched lldb-mi builds, retaining every MI transcript and measurement."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, help="optional eager-traversal build")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1..10")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    binaries = {"before": args.before.resolve(), "after": args.after.resolve()}
    if args.legacy:
        binaries["legacy"] = args.legacy.resolve()
    runner = Path(__file__).with_name("repro.py")
    runs = []

    def run(label, mode, count, suffix, extra=()):
        path = out / f"{label}-{mode}-{count}-{suffix}"
        if args.resume and (path / "report.json").exists():
            report = json.loads((path / "report.json").read_text())
            if report["sha256"] != hashlib.sha256(binaries[label].read_bytes()).hexdigest():
                raise RuntimeError(f"binary changed since {path}")
            if not report.get("error") and not report["guard_reason"]:
                if label == "after" and report.get("contract_failures"):
                    raise RuntimeError(f"failed after-build contracts in {path}")
                runs.append({"label": label, "returncode": 0,
                             "path": str(path), "report": report})
                return
            retry = 1
            while path.with_name(path.name + f"-retry{retry}").exists():
                retry += 1
            path = path.with_name(path.name + f"-retry{retry}")
        cmd = [sys.executable, str(runner), "--lldb-mi", str(binaries[label]),
               "--mode", mode, "--count", str(count), "--output", str(path),
               *extra]
        if label == "after":
            cmd.append("--expect-correct")
        with path.with_suffix(".log").open("w") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=120)
        report = json.loads((path / "report.json").read_text())
        runs.append({"label": label, "returncode": result.returncode,
                     "path": str(path), "report": report})
        print(json.dumps({"run": path.name, "returncode": result.returncode,
                          "failures": report.get("contract_failures", [])}),
              flush=True)
        if report.get("error") or report["guard_reason"] or (
                label == "after" and result.returncode):
            raise RuntimeError(f"benchmark failed; inspect {path}")

    for count in (8192, 32768):
        for repeat in range(args.repeats):
            labels = list(binaries)
            if repeat % 2:
                labels.reverse()
            for label in labels:
                run(label, "create", count, repeat)
    for label in ("before", "after"):
        for count in (256, 32768):
            run(label, "update", count, "polls", ("--cycles", "100"))
        run(label, "lifecycle", 8192, "variables", ("--cycles", "1000"))
        run(label, "lifecycle", 8192, "expressions",
            ("--cycles", "1000", "--expression", "(payload)"))

    rows = []
    for label in binaries:
        for count in (8192, 32768):
            group = [r["report"] for r in runs if r["label"] == label and
                     r["report"]["mode"] == "create" and r["report"]["count"] == count]
            elapsed = [next(c["elapsed_s"] * 1000 for c in r["commands"]
                            if c["command"].startswith("-var-create")) for r in group]
            growth = []
            for report in group:
                checkpoints = {c["label"]: c for c in report["checkpoints"]}
                growth.append((checkpoints["after_create"]["footprint_bytes"] -
                               checkpoints["before_create"]["footprint_bytes"]) / 2**20)
            rows.append({"build": label, "elements": count, "runs": len(group),
                         "create_ms_median": statistics.median(elapsed),
                         "create_ms_min": min(elapsed), "create_ms_max": max(elapsed),
                         "footprint_growth_mib_median": statistics.median(growth)})
    (out / "summary.json").write_text(json.dumps({"creation": rows, "runs": runs},
                                                indent=2) + "\n")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
