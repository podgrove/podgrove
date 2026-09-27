#!/usr/bin/env python3
"""Upload verified assets without replacement, rehash remote bytes, publish draft.

This script writes to GitHub only when explicitly invoked. A retry accepts an
existing asset only when its bytes match; published releases are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess

from resolve_release import resolve
from verify_release import sha256, verify_bundle


def download_digest(repository: str, asset: dict) -> str:
    ident = asset.get("id")
    if not isinstance(ident, int) or ident <= 0:
        raise ValueError("Invalid GitHub asset ID")
    result = subprocess.run(["gh", "api", f"repos/{repository}/releases/assets/{ident}",
                             "--header", "Accept: application/octet-stream"],
                            check=True, capture_output=True, timeout=180)
    return hashlib.sha256(result.stdout).hexdigest()


def publish(repository: str, dist: Path, *, expected_tag: str, expected_sha: str) -> None:
    manifest = verify_bundle(dist)
    tag = manifest["tag"]
    if tag != expected_tag or manifest["source_commit"] != expected_sha:
        raise ValueError("Recovery artifact does not match the explicitly requested release tag and commit")
    release, commit = resolve(repository, tag)
    if commit != manifest["source_commit"]:
        raise ValueError("Release tag and validated artifact source disagree")
    expected = {path.name: sha256(path) for path in dist.iterdir()}
    remote = {asset["name"]: asset for asset in release.get("assets", [])}
    if not set(remote).issubset(expected) or len(remote) != len(release.get("assets", [])):
        raise ValueError("Unexpected or duplicate assets on GitHub release; review manually")
    for name in sorted(expected):
        if name in remote:
            if download_digest(repository, remote[name]) != expected[name]:
                raise ValueError(f"Existing release asset differs; refusing replacement: {name}")
        elif release.get("draft"):
            subprocess.run(["gh", "release", "upload", tag, str(dist / name), "--repo", repository],
                           check=True, timeout=180)
        else:
            raise ValueError(f"Published release is missing {name}; publish a new version, never overwrite")
    current, current_commit = resolve(repository, tag)
    if current_commit != commit:
        raise ValueError("Release tag changed during upload")
    assets = current.get("assets", [])
    if len(assets) != len(expected) or {item["name"] for item in assets} != set(expected):
        raise ValueError("Remote release asset set differs after upload")
    for asset in assets:
        if download_digest(repository, asset) != expected[asset["name"]]:
            raise ValueError(f"Remote release asset hash mismatch: {asset['name']}")
    if current.get("draft"):
        subprocess.run(["gh", "release", "edit", tag, "--draft=false", "--repo", repository],
                       check=True, timeout=60)
    print(f"Verified published release {repository}@{tag}; no existing assets were replaced")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "podgrove/podgrove"))
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    publish(args.repository, args.dist.resolve(), expected_tag=args.tag, expected_sha=args.source_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
