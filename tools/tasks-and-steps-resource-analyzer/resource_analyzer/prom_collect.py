"""Batched per-pod Prometheus collection across clusters."""

import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Event, Lock, Semaphore, Thread
from typing import Any

from .clusters import extract_cluster_list, get_cluster_display_name
from .paths import TOOL_DIR
from .progress import _spinner_thread
from .prom import (
    _APPLICATION_LABEL_KEYS,
    _COMPONENT_LABEL_KEYS,
    DEBUG_SKIP_SAMPLE_LIMIT,
    POD_BATCH_SIZE,
    _component_fallback_from_names,
    _empty_collection_counters,
    _fill_component_cache_for_pods,
    _first_label_present,
    _list_task_pods,
    _merge_counters,
    _peaks_by_pod,
    _pod_regex_for_batch,
    _query_prometheus_instant,
    _series_peak_and_first_ts,
    format_lookback_label,
    format_promql_duration,
    resolve_lookback_seconds,
)
from .reporting import (
    _load_completed_partials,
    _save_cluster_partial,
)

try:
    import requests
    import urllib3
except ImportError:
    print(
        "Error: Missing required library. Install with: pip install requests pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def collect_individual_pod_executions(
    task_name: str,
    steps: list[str],
    days: int = 7,
    hours: int = 0,
    lookback_seconds: int | None = None,
    parallel_clusters: int | None = None,
    debug: bool = False,
    current_resources: dict[str, Any] | None = None,
    pll_queries: int = 2,
    pll_pods: int = 8,
) -> Any:
    """Collect individual pod execution data from all clusters.

    Each pod execution represents one pod run. For each pod, we get:
    - Max memory usage during pod lifetime
    - Max CPU usage during pod lifetime
    - Pod start timestamp (or first metric timestamp)
    - Current requests/limits from YAML when current_resources is provided

    Pods are listed once per cluster (not once per step). Returns
    (executions_list, collection_stats).

    Args:
        task_name: Task name
        steps: List of step names (without 'step-' prefix)
        days: Whole days in the lookback window
        hours: Additional hours in the lookback window (clubbed with days)
        lookback_seconds: Optional explicit lookback; overrides days/hours when set
        parallel_clusters: Number of parallel workers (None for serial)
        debug: Enable debug output including capped skip-reason samples
        current_resources: Optional dict step_name ->
            {requests: {memory, cpu}, limits: {memory, cpu}}
                          from extract_task_info(); used to add mem_requests_k8s, etc. to each
                          execution.
        pll_queries: Parallelism for the 4 metric queries within each pod batch (1-4).
        pll_pods: Parallelism for pod-batch jobs per cluster (each job is up to
            POD_BATCH_SIZE pods for one step/namespace).

    Returns:
        Tuple (list of execution dicts, collection_stats dict)
    """
    script_dir = TOOL_DIR
    wrapper_path = script_dir / "wrapper_for_promql_for_all_clusters.sh"

    empty_stats = {
        **_empty_collection_counters(),
        "per_cluster": {},
        "debug_samples": [],
        "lookback_seconds": 0,
        "lookback_label": "0",
    }

    if not wrapper_path.exists():
        return [], empty_stats

    if lookback_seconds is None:
        lookback_seconds = resolve_lookback_seconds(days, hours)
    lookback_seconds = int(lookback_seconds)
    range_str = format_promql_duration(lookback_seconds)
    lookback_label = format_lookback_label(days, hours)

    all_executions = []
    collection_stats = {
        **_empty_collection_counters(),
        "per_cluster": {},
        "debug_samples": [],
        "lookback_seconds": lookback_seconds,
        "lookback_label": lookback_label,
    }
    samples_lock = Lock()
    # Cap concurrent Prom HTTP across cluster workers to avoid stampedes.
    # Allow up to pll_pods batch jobs × pll_queries in-flight metric requests
    # per cluster (matches --pll-pods / --pll-queries as throughput knobs).
    _pll_q_cap = max(1, min(4, pll_queries or 2))
    _pll_pods_cap = max(1, pll_pods or 8)
    _cluster_cap = max(1, parallel_clusters or 1)
    prom_sem = Semaphore(max(8, _cluster_cap * _pll_q_cap * _pll_pods_cap))

    def add_debug_sample(
        reason: str,
        cluster_name: str,
        pod_name: str = "",
        namespace: str = "",
        step: str = "",
        detail: str = "",
    ) -> Any:
        if not debug:
            return
        with samples_lock:
            if len(collection_stats["debug_samples"]) >= DEBUG_SKIP_SAMPLE_LIMIT:
                return
            collection_stats["debug_samples"].append(
                {
                    "reason": reason,
                    "cluster": cluster_name,
                    "pod": pod_name,
                    "namespace": namespace,
                    "step": step,
                    "detail": detail,
                }
            )

    # Extract cluster list
    clusters_raw = extract_cluster_list(wrapper_path)
    if not clusters_raw:
        return [], collection_stats

    clusters = list(dict.fromkeys([c.strip() for c in clusters_raw if c and c.strip()]))

    # Resume: load any clusters already checkpointed from a prior interrupted run.
    # Skipped when --analyze-again is used (caller clears partials before calling us).
    already_done = _load_completed_partials(task_name)
    if already_done:
        print(
            f"\n[checkpoint] Resuming run — {len(already_done)} cluster(s) already saved: "
            + ", ".join(sorted(already_done)),
            file=sys.stderr,
        )
        for _cluster, (_execs, _stats) in already_done.items():
            all_executions.extend(_execs)
            collection_stats["per_cluster"][_cluster] = _stats
            _merge_counters(collection_stats, _stats)
        clusters = [c for c in clusters if get_cluster_display_name(c) not in already_done]
        if not clusters:
            print(
                "[checkpoint] All clusters already checkpointed — skipping data collection.",
                file=sys.stderr,
            )
            return all_executions, collection_stats

    def process_cluster_for_detailed_data(cluster_ctx: str) -> Any:
        """Process a single cluster to get detailed pod execution data."""
        cluster_stats = _empty_collection_counters()
        cluster_name = get_cluster_display_name(cluster_ctx)
        try:
            original_kubeconfig = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
            temp_kubeconfig = (
                script_dir
                / f".kubeconfig_detailed_{cluster_ctx.replace('/', '_').replace(':', '_')}"
            )

            try:
                if os.path.exists(original_kubeconfig):
                    shutil.copy2(original_kubeconfig, temp_kubeconfig)

                env = os.environ.copy()
                env["KUBECONFIG"] = str(temp_kubeconfig)

                subprocess.run(
                    ["kubectl", "config", "use-context", cluster_ctx],
                    capture_output=True,
                    env=env,
                    timeout=10,
                )

                token_result = subprocess.run(
                    ["oc", "whoami", "--show-token"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=10,
                )
                if token_result.returncode != 0:
                    cluster_stats["list_failures"] += 1
                    add_debug_sample(
                        "auth_failure",
                        cluster_name,
                        detail="oc whoami --show-token failed",
                    )
                    return [], cluster_stats

                token = token_result.stdout.strip()

                prom_result = subprocess.run(
                    [
                        "oc",
                        "-n",
                        "openshift-monitoring",
                        "get",
                        "route",
                        "prometheus-k8s",
                        "--no-headers",
                        "-o",
                        "custom-columns=HOST:.spec.host",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=10,
                )
                if prom_result.returncode != 0:
                    cluster_stats["list_failures"] += 1
                    add_debug_sample(
                        "prometheus_route_failure",
                        cluster_name,
                        detail="failed to get prometheus-k8s route",
                    )
                    return [], cluster_stats

                prom_host = prom_result.stdout.strip()
                if not prom_host:
                    cluster_stats["list_failures"] += 1
                    add_debug_sample(
                        "prometheus_route_failure",
                        cluster_name,
                        detail="empty prometheus host",
                    )
                    return [], cluster_stats

                end_time = int(time.time())

                cluster_executions = []

                # List pods once per cluster/task (not once per step).
                # Create a session per cluster for TLS connection reuse.
                prom_session = requests.Session()

                # (pod, namespace) -> first_ts from kube_pod_labels listing
                pod_first_ts = {}
                # (pod, namespace) -> (component, application)
                component_cache = {}

                try:
                    pods_raw = _list_task_pods(
                        prom_session,
                        prom_host,
                        token,
                        task_name,
                        end_time,
                        lookback_seconds,
                        sem=prom_sem,
                    )
                    cluster_stats["http_queries"] += 1
                    pods = []
                    seen = set()
                    if "data" in pods_raw and "result" in pods_raw["data"]:
                        for entry in pods_raw["data"]["result"]:
                            metric = entry.get("metric", {}) or {}
                            pod_name = metric.get("pod", "")
                            namespace = metric.get("namespace", "")
                            if not pod_name:
                                continue
                            key = (pod_name, namespace)
                            if key in seen:
                                continue
                            seen.add(key)
                            pods.append(key)
                            _peak, first_ts = _series_peak_and_first_ts(entry)
                            if first_ts is not None:
                                pod_first_ts[key] = first_ts
                            comp = _first_label_present(metric, _COMPONENT_LABEL_KEYS)
                            app = _first_label_present(metric, _APPLICATION_LABEL_KEYS)
                            component_cache[key] = (comp, app)
                except Exception as e:
                    # Count the failed list request so http_queries reflects traffic sent.
                    cluster_stats["http_queries"] += 1
                    cluster_stats["list_failures"] += 1
                    add_debug_sample(
                        "list_pods_failure",
                        cluster_name,
                        detail=str(e),
                    )
                    return [], cluster_stats

                cluster_stats["pods_listed"] = len(pods)
                if debug:
                    print(
                        f"DEBUG: Found {len(pods)} unique pods for task {task_name} "
                        f"in cluster {cluster_name} (lookback={lookback_label})",
                        file=sys.stderr,
                    )

                # Early exit: no pods on this cluster.
                if not pods:
                    return [], cluster_stats

                # Batch-fill component/application for pods missing labels on the list query.
                cluster_stats["http_queries"] += _fill_component_cache_for_pods(
                    prom_session,
                    prom_host,
                    token,
                    pods,
                    end_time,
                    lookback_seconds,
                    component_cache,
                    sem=prom_sem,
                )

                # Register with the spinner so it can show live pod-level progress.
                with progress_lock:
                    progress_data["active_clusters"][cluster_name] = {
                        "stats_ref": cluster_stats,
                        "total": len(pods) * len(steps),
                    }

                stats_lock = Lock()
                # Shared pool sized so --pll-pods × --pll-queries concurrent metric
                # requests can actually run (batch jobs would otherwise serialize
                # on a tiny pool capped at min(4, pll_queries)).
                _pll_pods_eff = max(1, pll_pods)
                query_pool_size = max(1, _pll_pods_eff * min(4, pll_queries))
                query_executor = ThreadPoolExecutor(max_workers=query_pool_size)

                def _run_instant_metric(metric_query_pair: tuple[str, str]) -> Any:
                    """Run one instant PromQL query; retry transient failures."""
                    metric_name, query = metric_query_pair
                    last_exc = None
                    for attempt in range(3):
                        try:
                            result = _query_prometheus_instant(
                                prom_session,
                                prom_host,
                                token,
                                query,
                                eval_time=end_time,
                                sem=prom_sem,
                            )
                            with stats_lock:
                                cluster_stats["http_queries"] += 1
                            return metric_name, result
                        except Exception as exc:
                            with stats_lock:
                                cluster_stats["http_queries"] += 1
                            last_exc = exc
                            if attempt < 2:
                                time.sleep(0.4 * (attempt + 1))
                    return metric_name, last_exc

                def _process_pod_batch(item: Any) -> Any:
                    """Process one (step, step_name, namespace, pod_batch) work item."""
                    step, step_name, namespace, pod_batch = item
                    records: list[dict[str, Any]] = []
                    with stats_lock:
                        cluster_stats["pods_queried"] += len(pod_batch)

                    regex = _pod_regex_for_batch(pod_batch)
                    if not regex:
                        return records

                    ns_clause = (
                        f'namespace="{namespace}"'
                        if namespace and namespace != "N/A"
                        else 'namespace=~".*-tenant"'
                    )
                    labels = f'container="{step_name}",pod=~"({regex})",{ns_clause}'
                    mem_query = (
                        f"max_over_time("
                        f"container_memory_working_set_bytes"
                        f"{{{labels}}}[{range_str}])"
                    )
                    cpu_query = (
                        f"max_over_time(rate("
                        f"container_cpu_usage_seconds_total"
                        f"{{{labels}}}[5m])"
                        f"[{range_str}:5m])"
                    )
                    io_read_query = (
                        f"max_over_time(rate("
                        f"container_fs_reads_bytes_total"
                        f"{{{labels}}}[5m])"
                        f"[{range_str}:5m])"
                    )
                    io_write_query = (
                        f"max_over_time(rate("
                        f"container_fs_writes_bytes_total"
                        f"{{{labels}}}[5m])"
                        f"[{range_str}:5m])"
                    )
                    query_pairs = [
                        ("mem", mem_query),
                        ("cpu", cpu_query),
                        ("io_read", io_read_query),
                        ("io_write", io_write_query),
                    ]
                    try:
                        query_results = dict(query_executor.map(_run_instant_metric, query_pairs))
                    except Exception as exc:
                        with stats_lock:
                            cluster_stats["query_failures"] += len(pod_batch)
                        add_debug_sample(
                            "query_failure",
                            cluster_name,
                            namespace=namespace,
                            step=step_name,
                            detail=str(exc),
                        )
                        return records

                    mem_result = query_results.get("mem")
                    cpu_result = query_results.get("cpu")
                    io_read_result = query_results.get("io_read")
                    io_write_result = query_results.get("io_write")

                    mem_ok = not isinstance(mem_result, Exception) and mem_result is not None
                    if not mem_ok:
                        with stats_lock:
                            cluster_stats["query_failures"] += len(pod_batch)
                        err_detail = (
                            f"{type(mem_result).__name__}: {mem_result}"
                            if isinstance(mem_result, Exception)
                            else "mem query returned None"
                        )
                        add_debug_sample(
                            "query_failure",
                            cluster_name,
                            namespace=namespace,
                            step=step_name,
                            detail=err_detail,
                        )
                        return records

                    try:
                        mem_by_pod = _peaks_by_pod(mem_result)
                        cpu_by_pod = _peaks_by_pod(
                            cpu_result
                            if not isinstance(cpu_result, Exception) and cpu_result is not None
                            else {}
                        )
                        io_read_by_pod = _peaks_by_pod(
                            io_read_result
                            if not isinstance(io_read_result, Exception)
                            and io_read_result is not None
                            else {}
                        )
                        io_write_by_pod = _peaks_by_pod(
                            io_write_result
                            if not isinstance(io_write_result, Exception)
                            and io_write_result is not None
                            else {}
                        )

                        res = (current_resources or {}).get(step, {}) or {}
                        req = res.get("requests") or {}
                        lim = res.get("limits") or {}
                        mem_req_k8s = req.get("memory") if req.get("memory") else "N/A"
                        cpu_req_k8s = req.get("cpu") if req.get("cpu") else "N/A"
                        mem_lim_k8s = lim.get("memory") if lim.get("memory") else "N/A"
                        cpu_lim_k8s = lim.get("cpu") if lim.get("cpu") else "N/A"

                        for pod_name in pod_batch:
                            key = (pod_name, namespace)
                            if pod_name not in mem_by_pod:
                                with stats_lock:
                                    cluster_stats["empty_metrics"] += 1
                                add_debug_sample(
                                    "empty_metrics",
                                    cluster_name,
                                    pod_name=pod_name,
                                    namespace=namespace,
                                    step=step_name,
                                    detail=f"no container_memory series for container={step_name}",
                                )
                                continue

                            mem_max, mem_ts = mem_by_pod[pod_name]
                            # Instant max_over_time samples are stamped at eval_time
                            # (≈ now), so they are useless for coverage/history.
                            # Prefer the first kube_pod_labels sample from the
                            # lookback range list; fall back to mem_ts only if
                            # the list query had no timestamp for this pod.
                            first_timestamp = pod_first_ts.get(key)
                            if first_timestamp is None:
                                first_timestamp = mem_ts
                            if mem_max == 0 and first_timestamp is None:
                                with stats_lock:
                                    cluster_stats["empty_metrics"] += 1
                                add_debug_sample(
                                    "empty_values",
                                    cluster_name,
                                    pod_name=pod_name,
                                    namespace=namespace,
                                    step=step_name,
                                    detail=f"matched series had no values container={step_name}",
                                )
                                continue

                            cpu_max = cpu_by_pod.get(pod_name, (0.0, None))[0]
                            io_read_max_bytes_s = io_read_by_pod.get(pod_name, (0.0, None))[0]
                            io_write_max_bytes_s = io_write_by_pod.get(pod_name, (0.0, None))[0]

                            component, application = component_cache.get(key, ("N/A", "N/A"))
                            if not component or component == "N/A":
                                component, application = _component_fallback_from_names(
                                    pod_name, namespace
                                )

                            mem_mb = mem_max / (1024 * 1024)
                            io_read_mbps = round(io_read_max_bytes_s / (1024 * 1024), 3)
                            io_write_mbps = round(io_write_max_bytes_s / (1024 * 1024), 3)

                            if first_timestamp:
                                exec_timestamp = datetime.fromtimestamp(first_timestamp).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                            else:
                                exec_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                            with stats_lock:
                                cluster_stats["pods_kept"] += 1

                            records.append(
                                {
                                    "task": task_name,
                                    "step": step,
                                    "component": component,
                                    "application": application,
                                    "cluster": cluster_name,
                                    "pod": pod_name,
                                    "namespace": namespace,
                                    "timestamp": exec_timestamp,
                                    "memory_mb": round(mem_mb, 2),
                                    "cpu_cores": round(cpu_max, 4),
                                    "io_read_mbps": io_read_mbps,
                                    "io_write_mbps": io_write_mbps,
                                    "mem_requests_k8s": mem_req_k8s,
                                    "cpu_requests_k8s": cpu_req_k8s,
                                    "mem_limits_k8s": mem_lim_k8s,
                                    "cpu_limits_k8s": cpu_lim_k8s,
                                }
                            )
                    except (KeyError, ValueError) as e:
                        with stats_lock:
                            cluster_stats["parse_errors"] += len(pod_batch)
                        add_debug_sample(
                            "parse_error",
                            cluster_name,
                            namespace=namespace,
                            step=step_name,
                            detail=str(e),
                        )
                        if debug:
                            print(
                                f"DEBUG: Error processing batch ns={namespace} "
                                f"step={step_name}: {e}",
                                file=sys.stderr,
                            )
                    return records

                # Batch jobs: (step, step_name, namespace, [pods...]) — transport only.
                pods_by_ns = defaultdict(list)
                for pod_name, namespace in pods:
                    pods_by_ns[namespace].append(pod_name)

                batch_jobs = []
                for step in steps:
                    step_name = f"step-{step}" if not step.startswith("step-") else step
                    for namespace, ns_pods in pods_by_ns.items():
                        for i in range(0, len(ns_pods), POD_BATCH_SIZE):
                            batch_jobs.append(
                                (step, step_name, namespace, ns_pods[i : i + POD_BATCH_SIZE])
                            )

                try:
                    with ThreadPoolExecutor(max_workers=_pll_pods_eff) as batch_exe:
                        for batch_records in batch_exe.map(_process_pod_batch, batch_jobs):
                            if batch_records:
                                cluster_executions.extend(batch_records)
                finally:
                    query_executor.shutdown(wait=True)

                return cluster_executions, cluster_stats

            finally:
                if temp_kubeconfig.exists():
                    temp_kubeconfig.unlink()

        except Exception as e:
            if debug:
                print(
                    f"DEBUG: Error processing cluster {cluster_ctx}: {e}",
                    file=sys.stderr,
                )
            cluster_stats["list_failures"] += 1
            add_debug_sample(
                "cluster_error",
                cluster_name,
                detail=str(e),
            )
            return [], cluster_stats

    total_clusters = len(set(get_cluster_display_name(c) for c in clusters))
    progress_data: dict[str, Any] = {
        "completed": [],
        "pods_listed": 0,
        "pods_queried": 0,
        "pods_kept": 0,
        # active_clusters: {cluster_display: {"stats_ref": cluster_stats_dict, "total": int}}
        # Populated by process_cluster_for_detailed_data() once pod list is known.
        # stats_ref is a live reference — spinner reads pods_queried directly without a lock
        # (GIL makes individual dict-key reads safe for a progress display).
        "active_clusters": {},
    }
    progress_lock = Lock()
    spinner_stop = Event()
    spinner_thread = Thread(
        target=_spinner_thread,
        args=(spinner_stop, progress_data, progress_lock, total_clusters),
        daemon=True,
    )
    spinner_thread.start()
    try:
        if parallel_clusters and parallel_clusters > 0:
            with ThreadPoolExecutor(max_workers=parallel_clusters) as executor:
                futures = {
                    executor.submit(process_cluster_for_detailed_data, cluster): cluster
                    for cluster in clusters
                }
                for future in as_completed(futures):
                    cluster_ctx = futures[future]
                    executions, cluster_stats = future.result()
                    display = get_cluster_display_name(cluster_ctx)
                    # Checkpoint to disk immediately — data is safe even if process dies later
                    _save_cluster_partial(task_name, display, executions, cluster_stats)
                    with progress_lock:
                        if display not in progress_data["completed"]:
                            progress_data["completed"].append(display)
                        progress_data["pods_listed"] += cluster_stats.get("pods_listed", 0)
                        progress_data["pods_queried"] += cluster_stats.get("pods_queried", 0)
                        progress_data["pods_kept"] += cluster_stats.get("pods_kept", 0)
                        # Remove from active_clusters now that it is done
                        progress_data["active_clusters"].pop(display, None)
                    collection_stats["per_cluster"][display] = cluster_stats
                    _merge_counters(collection_stats, cluster_stats)
                    all_executions.extend(executions)
        else:
            for cluster in clusters:
                executions, cluster_stats = process_cluster_for_detailed_data(cluster)
                display = get_cluster_display_name(cluster)
                # Checkpoint to disk immediately — data is safe even if process dies later
                _save_cluster_partial(task_name, display, executions, cluster_stats)
                with progress_lock:
                    if display not in progress_data["completed"]:
                        progress_data["completed"].append(display)
                    progress_data["pods_listed"] += cluster_stats.get("pods_listed", 0)
                    progress_data["pods_queried"] += cluster_stats.get("pods_queried", 0)
                    progress_data["pods_kept"] += cluster_stats.get("pods_kept", 0)
                    # Remove from active_clusters now that it is done
                    progress_data["active_clusters"].pop(display, None)
                collection_stats["per_cluster"][display] = cluster_stats
                _merge_counters(collection_stats, cluster_stats)
                all_executions.extend(executions)
    finally:
        spinner_stop.set()
        spinner_thread.join(timeout=1.0)
        print("=" * 80, file=sys.stderr)
        print(
            "Getting information from all clusters: (Completed No. of clusters: 100%)",
            file=sys.stderr,
        )
        print("=" * 80, file=sys.stderr)
        print(
            "Collection summary: "
            f"listed={collection_stats['pods_listed']} "
            f"queried={collection_stats['pods_queried']} "
            f"kept={collection_stats['pods_kept']} "
            f"http_queries={collection_stats.get('http_queries', 0)} "
            f"query_failures={collection_stats['query_failures']} "
            f"empty_metrics={collection_stats['empty_metrics']} "
            f"parse_errors={collection_stats['parse_errors']} "
            f"list_failures={collection_stats['list_failures']}",
            file=sys.stderr,
        )
        if collection_stats["per_cluster"]:
            print("Per-cluster collection stats:", file=sys.stderr)
            for cl in sorted(collection_stats["per_cluster"]):
                cs = collection_stats["per_cluster"][cl]
                print(
                    f"  {cl}: listed={cs['pods_listed']} queried={cs['pods_queried']} "
                    f"kept={cs['pods_kept']} http_queries={cs.get('http_queries', 0)} "
                    f"query_failures={cs['query_failures']} "
                    f"empty_metrics={cs['empty_metrics']}",
                    file=sys.stderr,
                )
        if debug and collection_stats["debug_samples"]:
            print(
                f"DEBUG: Skip samples "
                f"(showing {len(collection_stats['debug_samples'])}/"
                f"{DEBUG_SKIP_SAMPLE_LIMIT} capped):",
                file=sys.stderr,
            )
            for sample in collection_stats["debug_samples"]:
                print(
                    f"  [{sample['reason']}] cluster={sample['cluster']} "
                    f"step={sample['step']} ns={sample['namespace']} "
                    f"pod={sample['pod']} detail={sample['detail']}",
                    file=sys.stderr,
                )
        if all_executions:
            print(
                f"Collected data from {len(progress_data['completed'])} cluster(s), total pod"
                f" executions: {len(all_executions)}",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    return all_executions, collection_stats
