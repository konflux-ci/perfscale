---
status: Accepted
applies_to: perfscale
---

# 2. Parallel OOM/CrashLoop scan via oc contexts

Date: 2026-10-12

## Status

Accepted

## Context

Konflux spans many OpenShift clusters. Detecting OOMKilled and CrashLoopBackOff pods fleet-wide with serial `oc` calls is too slow for operational use. The detector must stay usable from an engineer laptop with existing kubeconfigs.

## Decision

Implement the OOM/CrashLoop detector as an **`oc`-based** tool with **constant cluster parallelism** and namespace batching (`tools/oomkill-and-crashloopbackoff-detector/`). Discover targets from `oc` contexts; collect forensic describe/logs at detection time; export CSV/JSON/HTML.

## Consequences

- Positive: works with existing kubeconfig / `oc` workflows; no custom cluster agent
- Positive: wall-clock time stays acceptable across large fleets
- Negative: API rate limits constrain how high `--batch` / `--ns-workers` can go
- Neutral: namespace attribution rules (drop event-only / wrong-kind) are required invariants
