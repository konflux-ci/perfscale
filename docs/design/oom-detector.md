# OOMKilled / CrashLoopBackOff detector

## Overview

`oc_get_ooms.py` scans one or many OpenShift contexts for OOMKilled and CrashLoopBackOff pods, collects forensic artifacts, and exports CSV/JSON/HTML/TABLE reports. Work is parallelized at cluster and namespace layers via a constant-size worker pool. See `tools/oomkill-and-crashloopbackoff-detector/README.md` for flags and operator steps.

```
oc contexts → cluster workers (--batch)
                → namespace batches (--ns-batch-size)
                     → namespace workers (--ns-workers)
                          → events + pod status → artifacts → export
```

## Preconditions

- A usable `kubeconfig` with `oc` contexts for every cluster to scan (`oc config get-contexts`).
- `oc` must be able to list namespaces, events, and pods in those contexts (connectivity check runs first).
- Time-range flags (`1h`, `1d`, …) apply to both events and pod-status findings that carry a usable termination timestamp.
- Artifact directories under the configured output path must be writable.

## Invariants

- **Constant cluster parallelism**: at most `--batch` cluster workers run at once; when one finishes, the next context starts immediately (no waiting for a full batch barrier).
- **Namespace attribution must hold**: findings are bound to the namespace being scanned. Event-only names that are not Pods, or pods missing from that namespace’s pod listing, are dropped (see `test_namespace_misattribution.py` / KONFLUX-14702).
- **Dual detection**: both Kubernetes events and live pod status contribute; duplicates are merged, not double-counted as independent incidents.
- **Ephemeral namespaces are skippable**: EaaS / test-style namespaces (labels or name patterns) can be excluded so short-lived CI noise does not dominate reports.
- **Artifact paths are stable and unique**: describe and log files include cluster, namespace, pod, and timestamp so parallel workers do not overwrite each other.
- **Exports reference artifacts absolutely**: CSV/JSON include absolute `description_file` / `pod_log_file` paths so reports remain usable after moving the summary files.
- **Application/Component enrichment is label-only**: labels such as `appstudio.openshift.io/application` are read from the pod object already fetched; no extra API round-trips for enrichment.

## Rationale

- **Cluster + namespace fan-out** matches how Konflux fleets are operated (many clusters, many namespaces) and keeps wall-clock time acceptable without unbounded API load.
- **Single events list per namespace, filter in memory** rather than per-pod event queries: fewer API calls, same detection coverage.
- **Drop event-only / wrong-kind objects** rather than trusting `involvedObject` blindly: prevents DaemonSet/Node noise and cross-namespace misattribution that would send operators to the wrong owners.
- **Forensic artifacts at detection time** capture describe/logs before pods disappear, which is common for OOM/CrashLoop workloads.

## Trade-offs

- Higher `--batch` / `--ns-workers` speeds large fleets but can trip API rate limits; defaults favor stability.
- Skipping ephemeral namespaces reduces noise at the cost of missing OOMs that only appear in short-lived test tenants.
- Per-pod tarball generation at end-of-run is convenient for handoff but expensive on huge result sets (`--no-tarballs` opts out).
