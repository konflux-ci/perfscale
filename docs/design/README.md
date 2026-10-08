# Design intent documentation

This directory documents **why** critical operational tools behave the way they do. Use it with tool READMEs (operator guides) and `AGENTS.md` (agent entrypoint).

## Doc map

| Doc | Scope | Key code |
|-----|-------|----------|
| [oom-detector.md](oom-detector.md) | Parallel OOMKilled / CrashLoopBackOff scan, namespace attribution, forensic artifacts | `tools/oomkill-and-crashloopbackoff-detector/` |
| [resource-analyzer.md](resource-analyzer.md) | Prometheus collection, step metrics, limit recommendations | `tools/tasks-and-steps-resource-analyzer/` |

## When to update

Review and update the matching design doc when you change cluster/namespace parallelism, event-to-pod attribution, artifact layout, PromQL queries, recommendation bases (MAX/P95/P90/Median), or YAML update behavior.
