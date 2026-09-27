#!/usr/bin/env python3
"""Resolve an existing stable GitHub release to an exact commit (read-only)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

from homebrew_formula import REPOSITORY, validated_version


def gh_json(*arguments: str) -> dict:
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


def resolve(repository: str, tag: str) -> tuple[dict, str]:
    if not REPOSITORY.fullmatch(repository) or not tag.startswith("v"):
        raise ValueError("Expected owner/repository and a stable vX.Y.Z tag")
    validated_version(tag[1:])
    release = gh_json("api", f"repos/{repository}/releases/tags/{tag}")
    if release.get("tag_name") != tag or release.get("prerelease"):
        raise ValueError("Only the requested stable release is supported")
    commit = gh_json("api", f"repos/{repository}/commits/{tag}").get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Release tag does not resolve to a commit")
    return release, commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "podgrove/podgrove"))
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""))
    parser.add_argument("--expected-sha", default=os.environ.get("RELEASE_SHA", ""))
    parser.add_argument("--artifact-run-id", default=os.environ.get("ARTIFACT_RUN_ID", ""))
    args = parser.parse_args()
    if args.artifact_run_id and not re.fullmatch(r"[1-9][0-9]*", args.artifact_run_id):
        parser.error("Artifact run ID must be a positive integer")
    _, commit = resolve(args.repository, args.tag)
    if args.expected_sha and args.expected_sha != commit:
        parser.error("Release tag moved or disagrees with release-please's commit")
    if args.artifact_run_id:
        run = gh_json("api", f"repos/{args.repository}/actions/runs/{args.artifact_run_id}")
        if (run.get("path") != ".github/workflows/release.yml" or run.get("event") != "push"
                or run.get("head_branch") != "main" or run.get("head_sha") != commit
                or run.get("repository", {}).get("full_name") != args.repository
                or run.get("head_repository", {}).get("full_name") != args.repository):
            parser.error("Recovery artifacts must come from the original main-branch Release push for this commit")
    outputs = {"tag": args.tag, "sha": commit, "version": args.tag[1:],
               "artifact_run_id": args.artifact_run_id or os.environ.get("GITHUB_RUN_ID", "")}
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
