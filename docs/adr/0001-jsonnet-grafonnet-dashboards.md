---
status: Accepted
applies_to: perfscale
---

# 1. Jsonnet/Grafonnet for dashboard-as-code

Date: 2026-10-12

## Status

Accepted

## Context

Perf&Scale maintains Grafana dashboards for Konflux probes and in-cluster monitoring. Hand-edited JSON from the Grafana UI drifts, is hard to review, and mixes environment-specific datasource fields into git. The team needs a reviewable, rebuildable source of truth for dashboards hosted on grafana.corp.redhat.com.

## Decision

Author grafana.corp dashboards as **Jsonnet/Grafonnet** under `grafonnet-workdir/`, rebuild with `grafonnet-workdir/build.sh` into `grafana/`, and treat Jsonnet as the long-term source of truth. Vendor Grafonnet deps via `jb` and commit `grafonnet-workdir/vendor/`.

## Consequences

- Positive: dashboards are diffable, rebuildable, and reviewable in PRs
- Positive: `jb install` only needed when updating vendor deps
- Negative: contributors need Jsonnet/jb tooling (`make bootstrap`)
- Neutral: some in-cluster JSON under `grafana/` may still be hand-managed until migrated
