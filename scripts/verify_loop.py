#!/usr/bin/env python3
"""Repeat finite verification rounds and preserve every failure; never mutate Git.

Default unit/lint loop has no cluster effects. --docker runs the owned local-engine
matrix. Cluster acceptance is a separate administrator-authorized procedure.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--output", type=Path, help="New or empty artifact directory; existing evidence is never overwritten")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds must be between 1 and 100")
    output = args.output or ROOT / "artifacts" / ("verification-loop-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be a new or empty directory so earlier evidence is retained")
    output.mkdir(parents=True, exist_ok=True)
    print(f"Verification logs: {output.resolve()}", flush=True)
    results = []
    commands = [
        ("lint", [sys.executable, "-m", "ruff", "check", "podgrove", "scripts", "tests"]),
        ("unit", [sys.executable, "-m", "pytest", "-q", "-m", "not integration and not cluster"]),
    ]
    if args.docker:
        commands.append(("docker", [sys.executable, "scripts/e2e.py", "--mongo"]))
    for round_number in range(1, args.rounds + 1):
        for name, command in commands:
            start = time.monotonic()
            with (output / f"round-{round_number}-{name}.log").open("w") as log:
                result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
            record = {"round": round_number, "check": name, "exit_code": result.returncode,
                      "seconds": round(time.monotonic() - start, 3)}
            results.append(record)
            (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(record), flush=True)
            if result.returncode:
                print(f"Failure retained at {output}; fix it before starting another round.", file=sys.stderr)
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
