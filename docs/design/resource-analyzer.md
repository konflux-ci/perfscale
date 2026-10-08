# Tasks and steps resource analyzer

## Overview

`analyze_resource_limits.py` (package `resource_analyzer/`) measures real Memory/CPU (and related) usage of Tekton task steps from Prometheus across Konflux clusters, then recommends Kubernetes `computeResources`. Phase 1 builds multi-base stats (MAX, P95, P90, Median); a later update path can patch YAML. Operator details live in `tools/tasks-and-steps-resource-analyzer/README.md`.

```
task YAML → confirm steps → PromQL per cluster/pod/step
                              → cache (.analyze_cache/)
                              → aggregates + HTML comparison
                              → optional YAML patch (explicit update path)
```

## Preconditions

- Valid `kubeconfig` / `oc` login for every cluster that should contribute samples (`oclogin-all` or manual login).
- Prometheus reachable from those clusters for the container metrics the tool queries (`container_memory_working_set_bytes`, CPU usage, optional FS I/O).
- Task YAML (local path or GitHub URL) must parse and expose the step names being analyzed.
- Lookback window (`--days` / `--hours`) must be non-zero in aggregate; default is about 7 days.

## Invariants

- **Phase 1 ignores `--base` for collection**: all bases (max, p95, p90, median) are computed together so comparison HTML can show them side by side.
- **Recommendations include a safety margin**: configured `--margin` (percent) is applied on top of the chosen base; margin changes do not rewrite raw collected samples.
- **Cache keys are task- and date-scoped**: re-runs reuse `.analyze_cache/` unless `--analyze-again` forces refresh; `--update` skips collection and only reuses Phase 1 outputs.
- **YAML is not mutated in the default analysis path**: operators choose a base from the comparison report; automatic file edits only happen on the explicit update path.
- **Step identity is normalized for compare**: step names from YAML and metrics are normalized before joining so Tekton naming drift does not silently drop series.
- **Cluster coverage is reported**: partial fleet success is visible; a missing cluster must not silently look like zero usage for that cluster’s workloads.
- **Parallelism is bounded**: `--pll-clusters`, `--pll-queries`, and `--pll-pods` cap concurrent PromQL/pod work so runs stay within API budget.

## Rationale

- **Prometheus working-set / usage metrics** reflect what clusters actually experienced, which is a better right-sizing signal than static guesswork in task YAML.
- **Multiple bases in one pass** lets reviewers pick a conservative (MAX) or tighter (P95/P90/Median) posture without re-querying the fleet.
- **Separate compare-then-patch flow** keeps accidental mass edits out of the common path; limit PRs stay human-reviewed.
- **Package split under `resource_analyzer/`** keeps CLI, PromQL collection, stats, reporting, and YAML update responsibilities separable for agents and reviewers.

## Trade-offs

- Wider `--days` improves confidence but lengthens PromQL cost and wall time.
- Aggressive `--pll-*` flags cut latency but can overload Prometheus or hit auth/rate limits on large fleets.
- Margin protects against regressions under bursty load at the cost of higher reserved capacity.
- Cached runs are fast but can hide fleet changes until `--analyze-again` is used.
