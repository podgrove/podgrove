# Preparing the public repository

`public-files.json` is the reviewed file list for public Git source and source distributions. It intentionally excludes workstation configuration, private rollout notes, internal acceptance harnesses, generated evidence and Git history. Package code, portable tests, public docs and release tooling remain included.

## Prepare and inspect a snapshot

Run from the source checkout, selecting new paths outside it:

```sh
python3 scripts/prepare_public.py --output /tmp/podgrove-public-candidate --report /tmp/podgrove-public-candidate.json
```

Both paths must be new and their parents must already exist. The exporter validates the entire file list before copying, refuses symlinks and nonregular files, preserves executable status and records exact SHA-256 digests. It never copies `.git`, invokes Git, uploads files, or changes the source checkout. The known-pattern content guard supplements human review; it is not a complete secret scanner.

Validate the exported tree itself, including its documentation links, test suite, built sdist/wheel and installed CLI. Review every file in the generated report. A passing source test cannot establish package parity or a successful Homebrew installation.

## Keep the publication boundary explicit

Add a new public file to the sorted `files` list and its corresponding `include` line in `MANIFEST.in`. Keep the initial `global-exclude *` line: it prevents stale build metadata from carrying unreviewed local files into the archive. The public source tests check this boundary and release verification checks the exact package member sets.

An initial public push must use a **new repository history created from the reviewed export**. Adding an ignore rule or deleting a file does not remove it from prior commits. Retain the local development repository and pinned runtime independently for rollback. Never push that private development history to the public remote.

After the initial migration, the public repository becomes the canonical development source. Keep private acceptance evidence outside it; future releases should build directly from reviewed public commits, following [the release runbook](../docs/releasing.md). The first source push and enabling release automation are separate actions. Automation starts disabled and the Homebrew command is available only after its release assets and tap formula exist.
