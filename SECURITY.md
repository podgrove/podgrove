# Security policy

Podgrove is a development tool. Its Docker engine is privileged, and developers receive namespace-wide workload-management permissions. Namespace separation and NetworkPolicies are useful safeguards, but they are not a hardened boundary against malicious tenants. Use an approved development cluster/namespace and review [platform permissions](deploy/README.md) and [known limits](docs/known-limits.md).

## Reporting a vulnerability

Use [GitHub's private vulnerability reporting form](https://github.com/podgrove/podgrove/security/advisories/new) when private reporting is enabled. If the form is unavailable, open a public issue asking the maintainers to enable a private channel, without exploit details, credentials or sensitive logs. Do not disclose the vulnerability in a public issue while arranging that channel.

Include the affected version, impact, minimal reproduction on a disposable environment, and suggested mitigation if known. Never test against other users' clusters or running stacks. There is no guaranteed response time; maintainers coordinate fixes and public advisories with reporters.

## Supported versions

Security fixes target the latest published release. Older versions may require an upgrade; no long-term support branch is currently promised. Coordinate `up --refresh` with active worktree owners because installed code does not replace an already-running supervisor automatically.
