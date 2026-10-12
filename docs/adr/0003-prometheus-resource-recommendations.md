---
status: Accepted
applies_to: perfscale
---

# 3. Prometheus-based Tekton resource recommendations

Date: 2026-10-12

## Status

Accepted

## Context

Tekton task YAML often carries static Memory/CPU limits that do not match real Konflux workload behavior, causing OOMs or waste. Guessing limits without fleet data is unreliable.

## Decision

Drive right-sizing from **Prometheus** metrics (`container_memory_working_set_bytes`, CPU usage, optional FS I/O) via `tools/tasks-and-steps-resource-analyzer/`. Compute MAX/P95/P90/Median with a configurable safety margin; keep default analysis **read-only** (compare HTML first; YAML patch only on an explicit update path).

## Consequences

- Positive: recommendations reflect observed fleet usage
- Positive: multi-base comparison lets reviewers pick conservatism vs efficiency
- Negative: runs need Prometheus access and valid cluster logins; can be slow
- Neutral: cache under `.analyze_cache/` trades freshness for speed unless `--analyze-again`
