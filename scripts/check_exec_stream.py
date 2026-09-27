#!/usr/bin/env python3
"""Check exact binary exec output on one explicitly selected running environment.

The selected Compose service must have Python. This starts only read-only Python
commands inside that service; it never starts, refreshes or removes a stack.
Outputs are hashed from private temporary files, not accumulated in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

PATTERN = bytes(range(256)) * 256
INPUT = b"podgrove-exec-stdin-eof-fixture\n"
MARKER = b"podgrove-exec-stderr-fixture"
REMOTE = """import sys
if sys.stdin.buffer.read() != b'podgrove-exec-stdin-eof-fixture\\n':
    sys.exit(91)
remaining, code = int(sys.argv[1]), int(sys.argv[2])
block = bytes(range(256)) * 256
while remaining:
    payload = block[:remaining]
    sys.stdout.buffer.write(payload)
    remaining -= len(payload)
sys.stdout.buffer.flush()
sys.stderr.write('podgrove-exec-stderr-fixture\\n')
sys.stderr.flush()
sys.exit(code)
"""


def expected_digest(size):
    digest = hashlib.sha256()
    while size:
        chunk = PATTERN[:size]
        digest.update(chunk)
        size -= len(chunk)
    return digest.hexdigest()


def digest_file(path):
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            count += len(chunk)
            digest.update(chunk)
    return count, digest.hexdigest()


def command(args, action, *suffix):
    return [str(args.binary), action, "--project-directory", str(args.project_directory),
            "--context", args.context, "--namespace", args.namespace, *suffix]


def status(args):
    result = subprocess.run(command(args, "status", "--json"), capture_output=True, timeout=90, check=True)
    data = json.loads(result.stdout)
    if (data.get("identity") != args.identity or data.get("context") != args.context
            or data.get("namespace") != args.namespace or data.get("status") != "ready"):
        raise ValueError("Selected environment must be ready and match the explicit identity, context and namespace")
    result = {key: data.get(key) for key in ("identity", "context", "namespace", "status", "pid")}
    identity = data.get("engine_identity")
    if isinstance(identity, dict):
        result["engine_identity"] = {key: identity.get(key) for key in ("state", "expected", "observed")}
    return result


def check_export(args, size, exit_code, directory):
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="export-", dir=directory) as temporary:
        output, errors = Path(temporary) / "output", Path(temporary) / "stderr"
        with output.open("wb") as stdout, errors.open("wb") as stderr:
            result = subprocess.run(command(args, "exec", args.service, "--", args.python,
                                             "-c", REMOTE, str(size), str(exit_code)),
                                    input=INPUT, stdout=stdout, stderr=stderr, timeout=args.timeout)
        count, digest = digest_file(output)
        # Only the fixed marker is retained in the report, never arbitrary
        # kubectl/authentication stderr or the user's command environment.
        with errors.open("rb") as stream:
            marker_found = MARKER in stream.read(1024 * 1024)
    return {"requested_bytes": size, "received_bytes": count, "sha256": digest,
            "expected_sha256": expected_digest(size), "exit": result.returncode, "expected_exit": exit_code,
            "stderr_marker": marker_found, "elapsed_seconds": round(time.monotonic() - start, 3),
            "passed": count == size and digest == expected_digest(size) and result.returncode == exit_code and marker_found}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--project-directory", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--python", default="python3", help="Python executable inside the selected service")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True, help="New private evidence directory")
    args = parser.parse_args()
    if not args.binary.is_absolute() or not args.binary.is_file() or not args.project_directory.is_dir():
        parser.error("Provide an existing absolute binary path and project directory")
    if not re.fullmatch(r"[a-f0-9]{12}", args.identity) or args.timeout <= 0:
        parser.error("Provide the exact 12-character environment identity and a positive timeout")
    args.binary = args.binary.resolve()
    args.project_directory = args.project_directory.resolve()
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(args.output, 0o700)
    report = {"binary": str(args.binary), "checks": [], "passed": False}
    try:
        report["before"] = status(args)
        for size, code in ((29284, 0), (8 * 1024**2, 0), (64 * 1024**2, 0), (64 * 1024**2, 0), (29284, 7)):
            check = check_export(args, size, code, args.output)
            report["checks"].append(check)
            if not check["passed"]:
                break  # An uncertain command is never replayed.
            check["after"] = status(args)
            if check["after"] != report["before"]:
                check["passed"] = False
                check["reason"] = "Session or engine identity changed"
                break
        report["passed"] = len(report["checks"]) == 5 and all(row["passed"] for row in report["checks"])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["error_type"] = type(exc).__name__
    finally:
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "evidence": str(args.output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
