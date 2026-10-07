from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from re import Pattern
from typing import Any

from oom_artifacts import is_artifact_meaningful, save_pod_artifacts
from oom_cluster import (
    check_cluster_connectivity,
    color,
    parse_timestamp_to_iso,
    run_oc_subcommand,
    short_cluster_name,
)
from oom_constants import (
    _EXCLUDE_PATTERNS,
    _INCLUDE_PATTERNS,
    _VERBOSE,
    BLUE,
    DEFAULT_NS_BATCH_SIZE,
    DEFAULT_NS_WORKERS,
    GREEN,
    RED,
    YELLOW,
)
from oom_detection import (
    _application_component_from_labels,
    crashloop_via_pods_oc,
    get_all_events_oc,
    get_pods_items,
    oomkilled_via_pods_oc,
)


def is_ephemeral_namespace(
    namespace_name: str, namespace_metadata: dict[str, Any] | None = None
) -> bool:
    """
    Detect if a namespace is an ephemeral test or cluster namespace.

    Detection methods (in order of reliability):
    1. Label-based detection (most reliable):
       - konflux-ci.dev/namespace-type: eaas (EaaS ephemeral namespaces)
       - Other ephemeral namespace labels
    2. Name pattern matching:
       - Ephemeral cluster namespaces: clusters-<uuid> pattern
       - Ephemeral test namespaces: test-*, e2e-*, ephemeral-*, ci-*, pr-*, temp-*

    Args:
        namespace_name: Name of the namespace
        namespace_metadata: Optional namespace metadata dict (from Kubernetes API)
                          If provided, labels will be checked for more reliable detection

    Returns True if the namespace matches ephemeral patterns or labels.
    """
    if not namespace_name:
        return False

    # Method 1: Check labels (most reliable - works even if namespace name is modified)
    if namespace_metadata:
        labels = namespace_metadata.get("labels", {})
        if labels:
            # Primary check: EaaS ephemeral namespace label (most reliable indicator)
            # konflux-ci.dev/namespace-type: eaas
            if labels.get("konflux-ci.dev/namespace-type") == "eaas":
                return True

            # Check for other ephemeral namespace label indicators
            # Look for labels that suggest ephemeral/test namespaces
            ephemeral_label_indicators = {
                "konflux-ci.dev/namespace-type": ["eaas", "ephemeral", "test"],
                "namespace-type": ["eaas", "ephemeral", "test"],
                "ephemeral": ["true", "yes"],
            }

            for label_key, label_value in labels.items():
                label_key_lower = label_key.lower()
                label_value_lower = str(label_value).lower()

                # Check if label key matches known ephemeral indicators
                for indicator_key, indicator_values in ephemeral_label_indicators.items():
                    if indicator_key in label_key_lower and any(
                        val in label_value_lower for val in indicator_values
                    ):
                        return True

    # Method 2: Name pattern matching (fallback if labels not available)
    # Ephemeral cluster namespaces: clusters-<uuid> pattern
    # UUID format: 8-4-4-4-12 hex digits (e.g., clusters-4e52ba17-c17b-4f35-b7e0-0215e63678a0)
    if re.match(
        r"^clusters-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        namespace_name,
        re.IGNORECASE,
    ):
        return True

    # Ephemeral test namespaces: common test/e2e/ephemeral patterns
    ephemeral_test_patterns = [
        r"^test-",
        r"^e2e-",
        r"^ephemeral-",
        r"^ci-",
        r"^pr-",
        r"^temp-",
        r"^tmp-",
        r"-test$",
        r"-e2e$",
        r"-ephemeral$",
    ]

    for pattern in ephemeral_test_patterns:
        if re.search(pattern, namespace_name, re.IGNORECASE):
            return True

    return False


def get_namespaces_for_context(
    context: str,
    retries: int,
    oc_timeout_seconds: int,
    include_patterns: list[Pattern] | None = None,
    exclude_patterns: list[Pattern] | None = None,
    exclude_ephemeral: bool = True,
) -> list[str]:
    """
    Get namespaces for a context, optionally filtered by include/exclude patterns.

    Args:
        exclude_ephemeral: If True, automatically exclude ephemeral test and cluster namespaces
                          (default: True for EaaS clusters)
    """
    subcmd = ["get", "ns", "-o", "json"]
    rc, out, err = run_oc_subcommand(
        context, subcmd, retries=retries, oc_timeout_seconds=oc_timeout_seconds
    )
    if rc != 0 or not out:
        return []
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as e:
        logging.warning(f"Failed to parse namespaces JSON: {e}")
        return []
    # Collect namespace names and metadata for ephemeral detection
    namespaces_with_metadata = []
    for item in obj.get("items", []):
        metadata = item.get("metadata", {})
        ns_name = metadata.get("name")
        if ns_name:
            namespaces_with_metadata.append((ns_name, metadata))

    filtered: list[str] = []
    for ns_name, ns_metadata in namespaces_with_metadata:
        # Exclude ephemeral namespaces if enabled (check both labels and name patterns)
        if exclude_ephemeral and is_ephemeral_namespace(ns_name, ns_metadata):
            if _VERBOSE:
                print(color(f"  [skip ephemeral] {ns_name}", YELLOW))
            continue

        include = True
        if include_patterns:
            include = any(p.search(ns_name) for p in include_patterns)
        if not include:
            if _VERBOSE:
                print(color(f"  [skip include filter] {ns_name}", YELLOW))
            continue
        if exclude_patterns and any(p.search(ns_name) for p in exclude_patterns):
            if _VERBOSE:
                print(color(f"  [skip exclude filter] {ns_name}", YELLOW))
            continue
        filtered.append(ns_name)
    return filtered


def namespace_worker_oc(
    context: str,
    namespace: str,
    retries: int,
    oc_timeout_seconds: int,
    time_range_seconds: int | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Process namespace to find OOMKilled and CrashLoopBackOff pods."""
    pod_map: dict[str, dict[str, Any]] = {}

    # OPTIMIZATION: Fetch events once instead of 3 separate API calls
    all_events = get_all_events_oc(
        context,
        namespace,
        retries=retries,
        oc_timeout_seconds=oc_timeout_seconds,
        time_range_seconds=time_range_seconds,
    )

    # Filter events in memory for OOMKilled, CrashLoop, and BackOff
    oom_events: list[dict[str, str]] = []
    crash_events: list[dict[str, str]] = []
    backoff_events: list[dict[str, str]] = []

    for ev in all_events:
        reason = ev.get("reason", "")
        reason_lower = reason.lower()
        involved = ev.get("involvedObject", {})
        # Only process Pod events — ignore Node, DaemonSet, Deployment, etc.
        # to prevent namespace misattribution (e.g. KONFLUX-14702).
        if involved.get("kind") != "Pod":
            continue
        pod = involved.get("name")
        ts = ev.get("eventTime") or ev.get("lastTimestamp") or ev.get("firstTimestamp")

        if not pod or not ts:
            continue

        event_data = {
            "pod": pod,
            "reason": reason,
            "timestamp": parse_timestamp_to_iso(ts),
        }

        if "oomkilled" in reason_lower:
            oom_events.append(event_data)
        elif "crashloop" in reason_lower:
            crash_events.append(event_data)
        elif reason_lower == "backoff":
            # Match only the exact Kubernetes "BackOff" restart reason (container
            # crash exponential back-off). The previous substring match
            # "backoff" in reason_lower also caught ImagePullBackOff and
            # ErrImageBackOff, which are image-pull failures — not crash loops —
            # causing false-positive CrashLoopBackOff reports (KONFLUX-13422).
            backoff_events.append(event_data)

    # Fetch pods once for both OOM/Crash detection and for
    # application/component labels (incl. event-only pods)
    pod_items = get_pods_items(context, namespace, retries, oc_timeout_seconds)
    labels_map: dict[str, tuple[str, str]] = {}
    for item in pod_items:
        name = item.get("metadata", {}).get("name")
        if name:
            labels_map[name] = _application_component_from_labels(
                item.get("metadata", {}).get("labels")
            )

    # Also check pod status directly for OOMKilled and CrashLoopBackOff (same time range)
    oom_pods = oomkilled_via_pods_oc(
        context,
        namespace,
        retries=retries,
        oc_timeout_seconds=oc_timeout_seconds,
        time_range_seconds=time_range_seconds,
        items=pod_items,
    )
    crash_pods = crashloop_via_pods_oc(
        context,
        namespace,
        retries=retries,
        oc_timeout_seconds=oc_timeout_seconds,
        time_range_seconds=time_range_seconds,
        items=pod_items,
    )

    for e in oom_events:
        p = e["pod"]
        pod_map.setdefault(
            p,
            {
                "pod": p,
                "oom_timestamps": [],
                "crash_timestamps": [],
                "sources": set(),
                "application": "",
                "component": "",
            },
        )
        pod_map[p]["oom_timestamps"].append(e.get("timestamp", ""))
        pod_map[p]["sources"].add("events")
        if p in labels_map:
            pod_map[p]["application"], pod_map[p]["component"] = labels_map[p]
    for e in crash_events + backoff_events:
        p = e["pod"]
        pod_map.setdefault(
            p,
            {
                "pod": p,
                "oom_timestamps": [],
                "crash_timestamps": [],
                "sources": set(),
                "application": "",
                "component": "",
            },
        )
        pod_map[p]["crash_timestamps"].append(e.get("timestamp", ""))
        pod_map[p]["sources"].add("events")
        if p in labels_map:
            pod_map[p]["application"], pod_map[p]["component"] = labels_map[p]
    # Add OOM pods found via pod status (they already have application/component in e)
    for e in oom_pods:
        p = e["pod"]
        pod_map.setdefault(
            p,
            {
                "pod": p,
                "oom_timestamps": [],
                "crash_timestamps": [],
                "sources": set(),
                "application": e.get("application", ""),
                "component": e.get("component", ""),
            },
        )
        pod_map[p]["oom_timestamps"].append(e.get("timestamp", ""))
        pod_map[p]["sources"].add("oc_get_pods")
        pod_map[p]["application"] = e.get("application", "") or pod_map[p].get("application", "")
        pod_map[p]["component"] = e.get("component", "") or pod_map[p].get("component", "")
    for e in crash_pods:
        p = e["pod"]
        pod_map.setdefault(
            p,
            {
                "pod": p,
                "oom_timestamps": [],
                "crash_timestamps": [],
                "sources": set(),
                "application": e.get("application", ""),
                "component": e.get("component", ""),
            },
        )
        pod_map[p]["crash_timestamps"].append(e.get("timestamp", ""))
        pod_map[p]["sources"].add("oc_get_pods")
        pod_map[p]["application"] = e.get("application", "") or pod_map[p].get("application", "")
        pod_map[p]["component"] = e.get("component", "") or pod_map[p].get("component", "")

    # Drop event-only pods that don't exist in the pod listing — they indicate
    # stale events or cross-namespace references that would cause namespace
    # misattribution in downstream reports (e.g. KONFLUX-14702).
    actual_pod_names = set(labels_map.keys())
    if pod_map and actual_pod_names:
        event_only = [
            p
            for p, info in pod_map.items()
            if info.get("sources", set()) == {"events"} and p not in actual_pod_names
        ]
        for p in event_only:
            del pod_map[p]

    if pod_map:
        out_ns: dict[str, dict[str, Any]] = {}
        for p, info in pod_map.items():
            out_ns[p] = {
                "pod": p,
                "oom_timestamps": sorted(list(set(info.get("oom_timestamps", [])))),
                "crash_timestamps": sorted(list(set(info.get("crash_timestamps", [])))),
                "sources": sorted(list(info.get("sources", []))),
                "application": info.get("application", ""),
                "component": info.get("component", ""),
            }
        return out_ns
    return None


def query_context(
    context: str,
    retries: int,
    oc_timeout_seconds: int,
    ns_batch_size: int = DEFAULT_NS_BATCH_SIZE,
    ns_workers: int = DEFAULT_NS_WORKERS,
    time_range_seconds: int | None = None,
    exclude_ephemeral: bool = True,
    artifacts_root: Path | None = None,
) -> tuple[str, dict[str, Any], str | None]:
    cluster = short_cluster_name(context)
    print(color(f"\n→ Processing cluster: {cluster}", BLUE))

    ok, msg = check_cluster_connectivity(
        context, retries=retries, oc_timeout_seconds=oc_timeout_seconds
    )
    if not ok:
        err_msg = f"Cluster {cluster} unreachable or auth/connectivity failure: {msg}"
        print(color(f"  [SKIP] {err_msg}", RED))
        return cluster, {}, err_msg

    # Access global patterns (set in parse_args)
    namespaces = get_namespaces_for_context(
        context,
        retries=retries,
        oc_timeout_seconds=oc_timeout_seconds,
        include_patterns=_INCLUDE_PATTERNS,
        exclude_patterns=_EXCLUDE_PATTERNS,
        exclude_ephemeral=exclude_ephemeral,
    )
    if not namespaces:
        return cluster, {}, None

    if _VERBOSE:
        print(color(f"  Will scan {len(namespaces)} namespaces:", BLUE))
        for ns in namespaces:
            print(color(f"    {ns}", BLUE))

    cluster_result: dict[str, Any] = {}

    total_ns = len(namespaces)
    for i in range(0, total_ns, ns_batch_size):
        ns_batch = namespaces[i : i + ns_batch_size]
        print(
            color(
                f"  Namespace batch {i // ns_batch_size + 1}: {len(ns_batch)} namespaces",
                YELLOW,
            )
        )
        if _VERBOSE:
            print(color(f"    Scanning: {', '.join(ns_batch)}", BLUE))

        with ThreadPoolExecutor(max_workers=min(ns_workers, len(ns_batch))) as ex:
            futures = {
                ex.submit(
                    namespace_worker_oc,
                    context,
                    ns,
                    retries,
                    oc_timeout_seconds,
                    time_range_seconds,
                ): ns
                for ns in ns_batch
            }
            for fut in as_completed(futures):
                ns = futures[fut]
                try:
                    res = fut.result()
                    if res:
                        # Save artifacts for each pod found in this namespace
                        out_ns_with_artifacts: dict[str, dict[str, Any]] = {}
                        skipped = 0
                        for p, info in res.items():
                            if artifacts_root is not None:
                                desc_file, log_file = save_pod_artifacts(
                                    context,
                                    cluster,
                                    ns,
                                    p,
                                    retries,
                                    oc_timeout_seconds,
                                    artifacts_root=artifacts_root,
                                )
                                # Validate artifacts - skip if pod was deleted/not found
                                try:
                                    desc_content = Path(desc_file).read_text()
                                    log_content = Path(log_file).read_text()
                                    if not is_artifact_meaningful(
                                        desc_content
                                    ) or not is_artifact_meaningful(log_content):
                                        Path(desc_file).unlink(missing_ok=True)
                                        Path(log_file).unlink(missing_ok=True)
                                        skipped += 1
                                        continue
                                except Exception:  # nosec B110
                                    pass  # Keep pod if validation fails
                                info["description_file"] = desc_file
                                info["pod_log_file"] = log_file
                            else:
                                info["description_file"] = ""
                                info["pod_log_file"] = ""
                            out_ns_with_artifacts[p] = info
                        if out_ns_with_artifacts:
                            cluster_result[ns] = out_ns_with_artifacts
                        msg = f"    Namespace {ns}: {len(out_ns_with_artifacts)} pod(s) kept"
                        if skipped > 0:
                            msg += f" ({skipped} skipped - pod deleted)"
                        print(color(msg, YELLOW))
                except Exception as e:
                    print(color(f"    Error processing namespace {ns}: {e}", RED))

    # write per-cluster log under artifacts_root/<cluster>/ if available
    if artifacts_root:
        try:
            cluster_dir = (artifacts_root / cluster).resolve()
            cluster_dir.mkdir(parents=True, exist_ok=True)
            outfile = cluster_dir / f"{cluster}.log"
            outfile.write_text(json.dumps(cluster_result, indent=2))
        except Exception:  # nosec B110
            pass

    return cluster, cluster_result, None


def run_batches(
    contexts: list[str],
    batch_size: int,
    retries: int,
    oc_timeout_seconds: int,
    ns_batch_size: int,
    ns_workers: int,
    time_range_seconds: int | None = None,
    exclude_ephemeral: bool = True,
    output_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Run cluster processing with constant parallelism.

    Instead of processing in fixed batches, maintains constant parallelism:
    when one cluster finishes, immediately start the next one.
    """
    artifacts_root = (
        output_dir if output_dir is not None else Path("output")
    ).resolve() / "logs_and_description_files"
    results: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    total = len(contexts)
    context_index = 0
    active_futures: dict[Any, str] = {}

    with ThreadPoolExecutor(max_workers=batch_size) as ex:
        # Start initial batch
        while context_index < total and len(active_futures) < batch_size:
            ctx = contexts[context_index]
            context_index += 1
            fut = ex.submit(
                query_context,
                ctx,
                retries,
                oc_timeout_seconds,
                ns_batch_size,
                ns_workers,
                time_range_seconds,
                exclude_ephemeral,
                artifacts_root,
            )
            active_futures[fut] = ctx
            print(
                color(
                    f"Started processing cluster: {short_cluster_name(ctx)}",
                    BLUE,
                )
            )

        # Process as they complete, starting new ones to maintain parallelism
        while active_futures:
            for fut in as_completed(active_futures):
                ctx = active_futures.pop(fut)
                try:
                    cluster, data, err = fut.result()
                    if err:
                        skipped[cluster] = err
                        print(color(f"Skipped cluster {cluster}: {err}", RED))
                    else:
                        results[cluster] = data
                        print(color(f"Completed cluster {cluster}", GREEN))
                except Exception as e:
                    cluster_guess = short_cluster_name(ctx)
                    skipped[cluster_guess] = str(e)
                    print(color(f"Error processing {cluster_guess}: {e}", RED))

                # Start next cluster if available
                if context_index < total:
                    next_ctx = contexts[context_index]
                    context_index += 1
                    next_fut = ex.submit(
                        query_context,
                        next_ctx,
                        retries,
                        oc_timeout_seconds,
                        ns_batch_size,
                        ns_workers,
                        time_range_seconds,
                        exclude_ephemeral,
                        artifacts_root,
                    )
                    active_futures[next_fut] = next_ctx
                    print(
                        color(
                            f"Started processing cluster: {short_cluster_name(next_ctx)}",
                            BLUE,
                        )
                    )

    return results, skipped


def collect_rows(results: dict[str, Any], time_range_str: str = "1d") -> list[dict[str, str]]:
    """
    Collect all rows from results dictionary.

    Returns a list of dictionaries representing rows, sorted by type
    (OOMKilled first, then CrashLoopBackOff).
    """
    rows = []
    # Skip _metadata if present
    for cluster, ns_map in results.items():
        if cluster == "_metadata":
            continue
        for ns, pods in ns_map.items():
            for pod_name, info in pods.items():
                desc = info.get("description_file", "")
                plog = info.get("pod_log_file", "")
                sources = ";".join(info.get("sources", [])) if info.get("sources") else ""
                application = info.get("application", "")
                component = info.get("component", "")
                # OOM rows
                if info.get("oom_timestamps"):
                    rows.append(
                        {
                            "cluster": cluster,
                            "namespace": ns,
                            "pod": pod_name,
                            "type": "OOMKilled",
                            "application": application,
                            "component": component,
                            "timestamps": ";".join(info.get("oom_timestamps")),
                            "sources": sources,
                            "description_file": desc,
                            "pod_log_file": plog,
                            "time_range": time_range_str,
                        }
                    )
                # Crash rows
                if info.get("crash_timestamps"):
                    rows.append(
                        {
                            "cluster": cluster,
                            "namespace": ns,
                            "pod": pod_name,
                            "type": "CrashLoopBackOff",
                            "application": application,
                            "component": component,
                            "timestamps": ";".join(info.get("crash_timestamps")),
                            "sources": sources,
                            "description_file": desc,
                            "pod_log_file": plog,
                            "time_range": time_range_str,
                        }
                    )

    # Sort: OOMKilled first, then CrashLoopBackOff
    def sort_key(row: dict[str, str]) -> tuple[int, str, str, str]:
        type_val = row.get("type", "")
        if type_val == "OOMKilled":
            return (
                0,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )
        elif type_val == "CrashLoopBackOff":
            return (
                1,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )
        else:
            return (
                2,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )

    rows.sort(key=sort_key)
    return rows
