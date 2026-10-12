---
status: Accepted
applies_to: perfscale
---

# 5. Strip Grafana datasource fields before commit

Date: 2026-10-12

## Status

Accepted

## Context

Dashboards exported from the Grafana UI embed environment-specific `"datasource": {...}` blocks. Committing those fields breaks reuse across clusters and pollutes diffs. In-cluster delivery also depends on infra-deployments picking up a commit SHA.

## Decision

Require **`cleanup-dashboard.sh`** (or equivalent) to strip datasource fields before committing dashboard JSON. After merge, update the infra-deployments kustomization ref with `update-infra-ref.sh <sha>` (expects a sibling `../infra-deployments` checkout).

## Consequences

- Positive: JSON stays portable across Grafana instances
- Positive: clear handoff from this repo into infra-deployments
- Negative: easy to forget cleanup/ref update without AGENTS.md / checklist reminders
- Neutral: Grafonnet-built dashboards should still avoid baking in cluster-local datasources
