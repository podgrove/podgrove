#!/usr/bin/env python3
"""Resolve an existing stable GitHub release to an exact commit (read-only)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

from homebrew_formula import REPOSITORY, validated_version


RELEASE_PAGE_SIZE = 100
MAX_RELEASE_PAGES = 10


class GitHubAPIError(subprocess.CalledProcessError):
    def __init__(self, result: subprocess.CompletedProcess, status: int | None):
        super().__init__(result.returncode or 1, result.args, output=result.stdout, stderr=result.stderr)
        self.status = status


def gh_json(*arguments: str) -> dict | list:
    # Read the actual HTTP status, never infer a 404 from an error body/message.
    # --include is supported by gh api on both successful and failed requests.
    result = subprocess.run(["gh", *arguments, "--include"], check=False,
                            capture_output=True, text=True, timeout=60)
    headers, separator, payload = result.stdout.replace("\r\n", "\n").partition("\n\n")
    first = headers.split("\n", 1)[0]
    match = re.fullmatch(r"HTTP/[0-9]+(?:\.[0-9]+)? ([0-9]{3})(?: [^\n]*)?", first)
    status = int(match[1]) if match else None
    if result.returncode or status is None or not 200 <= status < 300:
        raise GitHubAPIError(result, status)
    if not separator:
        raise ValueError("GitHub API response has no header/body boundary")
    return json.loads(payload)


def _release_by_tag(repository: str, tag: str) -> dict:
    try:
        return gh_json("api", f"repos/{repository}/releases/tags/{tag}")
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise
    # The tag endpoint can omit authenticated drafts. Use the same gh auth for
    # a bounded listing, require one exact tag, then re-read its constructed ID
    # endpoint. Never follow URLs from the listing or select a prefix/latest.
    matches = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        releases = gh_json("api", f"repos/{repository}/releases?per_page={RELEASE_PAGE_SIZE}&page={page}")
        if (not isinstance(releases, list) or len(releases) > RELEASE_PAGE_SIZE
                or any(not isinstance(item, dict) or not isinstance(item.get("tag_name"), str)
                       or type(item.get("id")) is not int or item["id"] <= 0 for item in releases)):
            raise ValueError("GitHub release listing is malformed")
        matches.extend(item for item in releases if item["tag_name"] == tag)
        if len(matches) > 1:
            raise ValueError("Requested release tag is ambiguous in the GitHub listing")
        if len(releases) < RELEASE_PAGE_SIZE:
            break
    else:
        raise ValueError("GitHub release listing exceeded the bounded lookup; refusing an incomplete match")
    if len(matches) != 1:
        raise ValueError("Requested release tag was not found in the authenticated GitHub listing")
    ident = matches[0]["id"]
    release = gh_json("api", f"repos/{repository}/releases/{ident}")
    if not isinstance(release, dict) or release.get("id") != ident:
        raise ValueError("GitHub release ID changed during lookup")
    return release


def resolve(repository: str, tag: str) -> tuple[dict, str]:
    if not REPOSITORY.fullmatch(repository) or not tag.startswith("v"):
        raise ValueError("Expected owner/repository and a stable vX.Y.Z tag")
    validated_version(tag[1:])
    release = _release_by_tag(repository, tag)
    if not isinstance(release, dict) or release.get("tag_name") != tag or release.get("prerelease"):
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
