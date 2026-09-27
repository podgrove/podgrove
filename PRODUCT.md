# Podgrove browser workspace

## Register

product

## Users

Developers running Docker Compose worktrees on Kubernetes through Podgrove. They keep this local browser window beside their terminal to inspect services, endpoints, persistent storage, logs and configuration.

## Product Purpose

Provide a read-only view of this laptop's Podgrove environments in an explicitly selected cluster context. Show observed state honestly, including disconnected sessions and unavailable data. Lifecycle actions remain in the CLI.

## Brand Personality

Calm, familiar, precise. The user selected the original sidebar design (demo 01), with both light and dark themes. They requested Claude-like buttons and overall feel; Podgrove retains its own identity. Neutral surfaces, restrained terracotta accents, comfortable rounded controls, and deliberate typography support this direction.

## Anti-references

No marketing dashboard, invented metrics, decorative charts, dense wall of statistic cards, or pretend mutation controls. No imitation Claude branding.

## Design Principles

- Worktrees are the navigation unit; Services, Endpoints, Storage, Engine and Logs are views within one worktree.
- Distinguish local snapshots from fresh cluster observations.
- Keep selection and refresh predictable; preserve useful data when a refresh fails.
- Bundle every asset locally and make missing credentials or disconnected sessions actionable.
- Let developers collapse the sidebar without losing the Configuration button. Remember the sidebar choice and support switching worktrees from a mobile menu.
- Keep many endpoints manageable in their own searchable tab. Show saved addresses honestly without implying reachability, and identify omitted entries.

## Configuration

A button at the bottom of the sidebar opens a full read-only Configuration page. It shows the selected cluster context, the namespaces already in local or explicit scope, and observed namespace/access setup. Expandable entries distinguish existing, missing and unreadable Podgrove identities and RBAC declarations. These declarations do not claim effective permissions or successful bootstrap installation.

Configuration tabs and a worktree dropdown show safe current YAML settings separately from that environment's saved runtime state. Developers can understand configuration without applying changes from the browser. An unavailable file or cluster read should explain the limit and preserve the useful information already visible.

## Accessibility & Inclusion

Implementation baseline: keyboard navigation, visible focus, labelled controls, semantic tables, text alongside status color, readable contrast, reduced motion, and responsive layouts from 320px. These are implementation defaults, not a claim of formal WCAG certification.

## Control consistency

Focused controls use one continuous visible boundary, with no separated outer ring around an existing border. Search, dropdowns, buttons and keyboard navigation retain visible focus in both themes without changing layout.
