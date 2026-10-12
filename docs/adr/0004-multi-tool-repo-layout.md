---
status: Accepted
applies_to: perfscale
---

# 4. Multi-tool repository layout under tools/

Date: 2026-10-12

## Status

Accepted

## Context

This repository is a collection of Grafana/ops artifacts and Python helpers, not a single installable Python package. Forcing a `src/` + tests package layout would not improve navigation and would misrepresent how the code is used.

## Decision

Keep a **multi-tool layout**: dashboards in `grafana/` / `grafonnet-workdir/`, Terraform in `infra/`, operational scripts in `tools/<tool-name>/`. Exclude agentready `standard_layout` for this repo (see `.agentready-config.yaml`).

## Consequences

- Positive: matches how Perf engineers find and run tools
- Positive: avoids artificial package packaging for CLI scripts
- Negative: shared Python packaging / lockfile story is weaker than a single app
- Neutral: each tool may carry its own README and local deps
