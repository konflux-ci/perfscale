from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread

from arl_comparison_io import _list_task_pods, _query_prometheus_range, extract_component_from_pod
from arl_constants import (
    DEBUG_SKIP_SAMPLE_LIMIT,
    requests,
)
from arl_metrics import (
    _empty_collection_counters,
    _merge_counters,
    _spinner_thread,
    extract_cluster_list,
    format_lookback_label,
    format_promql_duration,
    get_cluster_display_name,
    resolve_lookback_seconds,
)
from arl_progress import _load_completed_partials, _save_cluster_partial


def collect_individual_pod_executions(
    task_name,
    steps,
    days=7,
    hours=0,
    lookback_seconds=None,
    parallel_clusters=None,
    debug=False,
    current_resources=None,
    pll_queries=2,
    pll_pods=8,
):
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
        pll_queries: Number of Prometheus queries to run in parallel per pod (1-4).

    Returns:
        Tuple (list of execution dicts, collection_stats dict)
    """
    script_dir = Path(__file__).parent
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
    # get_component_for_pod.py still expects integer days
    component_days = max(1, (lookback_seconds + 86399) // 86400)

    all_executions = []
    collection_stats = {
        **_empty_collection_counters(),
        "per_cluster": {},
        "debug_samples": [],
        "lookback_seconds": lookback_seconds,
        "lookback_label": lookback_label,
    }
    samples_lock = Lock()

    def add_debug_sample(reason, cluster_name, pod_name="", namespace="", step="", detail=""):
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

    def process_cluster_for_detailed_data(cluster_ctx):
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
                start_time = end_time - lookback_seconds

                cluster_executions = []

                # List pods once per cluster/task (not once per step).
                # Create a session per cluster for TLS connection reuse.
                prom_session = requests.Session()

                try:
                    pods_raw = _list_task_pods(
                        prom_session,
                        prom_host,
                        token,
                        task_name,
                        end_time,
                        lookback_seconds,
                    )
                    pods = []
                    seen = set()
                    if "data" in pods_raw and "result" in pods_raw["data"]:
                        for entry in pods_raw["data"]["result"]:
                            pod_name = entry.get("metric", {}).get("pod", "")
                            namespace = entry.get("metric", {}).get("namespace", "")
                            if not pod_name:
                                continue
                            key = (pod_name, namespace)
                            if key in seen:
                                continue
                            seen.add(key)
                            pods.append(key)
                except Exception as e:
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

                # Register with the spinner so it can show live pod-level progress.
                # stats_ref is a shared reference — spinner reads pods_queried directly.
                with progress_lock:
                    progress_data["active_clusters"][cluster_name] = {
                        "stats_ref": cluster_stats,
                        "total": len(pods) * len(steps),
                    }

                pod_lock = Lock()

                def _run_metric_query(metric_query_pair):
                    """Run one PromQL query in-process; retry transient failures."""
                    metric_name, query = metric_query_pair
                    last_exc = None
                    for attempt in range(3):
                        try:
                            return metric_name, _query_prometheus_range(
                                prom_session,
                                prom_host,
                                token,
                                query,
                                start_time,
                                end_time,
                            )
                        except Exception as exc:
                            last_exc = exc
                            if attempt < 2:
                                time.sleep(0.4 * (attempt + 1))
                    return metric_name, last_exc

                def _process_pod_step(item):
                    """Process one (pod_name, namespace, step, step_name) work item."""
                    pod_name, namespace, step, step_name = item
                    with pod_lock:
                        cluster_stats["pods_queried"] += 1

                    ns_filter = 'namespace=~".*-tenant"'
                    labels = f'container="{step_name}",pod="{pod_name}",{ns_filter}'
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
                    _pll = max(1, min(4, pll_queries))
                    with ThreadPoolExecutor(max_workers=_pll) as qex:
                        query_results = dict(qex.map(_run_metric_query, query_pairs))

                    mem_result = query_results.get("mem")
                    cpu_result = query_results.get("cpu")
                    io_read_result = query_results.get("io_read")
                    io_write_result = query_results.get("io_write")

                    # Memory is required; CPU/IO failures are non-fatal.
                    mem_ok = not isinstance(mem_result, Exception) and mem_result is not None
                    if not mem_ok:
                        with pod_lock:
                            cluster_stats["query_failures"] += 1
                        err_detail = (
                            f"{type(mem_result).__name__}: {mem_result}"
                            if isinstance(mem_result, Exception)
                            else "mem query returned None"
                        )
                        add_debug_sample(
                            "query_failure",
                            cluster_name,
                            pod_name=pod_name,
                            namespace=namespace,
                            step=step_name,
                            detail=err_detail,
                        )
                        return None

                    try:
                        mem_data = mem_result
                        cpu_data = (
                            cpu_result
                            if not isinstance(cpu_result, Exception) and cpu_result is not None
                            else {"data": {"result": []}}
                        )
                        io_read_data = (
                            io_read_result
                            if not isinstance(io_read_result, Exception)
                            and io_read_result is not None
                            else {}
                        )
                        io_write_data = (
                            io_write_result
                            if not isinstance(io_write_result, Exception)
                            and io_write_result is not None
                            else {}
                        )

                        mem_max = 0
                        cpu_max = 0
                        io_read_max_bytes_s = 0
                        io_write_max_bytes_s = 0
                        first_timestamp = None

                        mem_series = mem_data.get("data", {}).get("result", [])
                        cpu_series = cpu_data.get("data", {}).get("result", [])
                        io_read_series = io_read_data.get("data", {}).get("result", [])
                        io_write_series = io_write_data.get("data", {}).get("result", [])

                        matched_mem_series = False
                        if mem_series:
                            for series in mem_series:
                                metric = series.get("metric", {})
                                if metric.get("pod") == pod_name:
                                    matched_mem_series = True
                                    values = series.get("values", [])
                                    if values:
                                        if first_timestamp is None:
                                            first_timestamp = float(values[0][0])
                                        for _ts, val in values:
                                            mem_bytes = float(val) if val else 0
                                            if mem_bytes > mem_max:
                                                mem_max = mem_bytes

                        if cpu_series:
                            for series in cpu_series:
                                metric = series.get("metric", {})
                                if metric.get("pod") == pod_name:
                                    values = series.get("values", [])
                                    for _ts, val in values:
                                        cpu_val = float(val) if val else 0
                                        if cpu_val > cpu_max:
                                            cpu_max = cpu_val

                        for series in io_read_series:
                            if series.get("metric", {}).get("pod") == pod_name:
                                for _ts, val in series.get("values", []):
                                    v = float(val) if val else 0
                                    if v > io_read_max_bytes_s:
                                        io_read_max_bytes_s = v

                        for series in io_write_series:
                            if series.get("metric", {}).get("pod") == pod_name:
                                for _ts, val in series.get("values", []):
                                    v = float(val) if val else 0
                                    if v > io_write_max_bytes_s:
                                        io_write_max_bytes_s = v

                        if mem_max == 0 and first_timestamp is None:
                            with pod_lock:
                                cluster_stats["empty_metrics"] += 1
                            if mem_series and not matched_mem_series:
                                reason = "pod_label_mismatch"
                                detail = (
                                    f"mem_series={len(mem_series)} but none matched "
                                    f"pod={pod_name} container={step_name}"
                                )
                            elif mem_series and matched_mem_series:
                                reason = "empty_values"
                                detail = f"matched series had no values container={step_name}"
                            else:
                                reason = "empty_metrics"
                                detail = f"no container_memory series for container={step_name}"
                            add_debug_sample(
                                reason,
                                cluster_name,
                                pod_name=pod_name,
                                namespace=namespace,
                                step=step_name,
                                detail=detail,
                            )
                            return None

                        component, application = extract_component_from_pod(
                            pod_name,
                            namespace,
                            token,
                            prom_host,
                            end_time,
                            component_days,
                            session=prom_session,
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

                        res = (current_resources or {}).get(step, {}) or {}
                        req = res.get("requests") or {}
                        lim = res.get("limits") or {}
                        mem_req_k8s = req.get("memory") if req.get("memory") else "N/A"
                        cpu_req_k8s = req.get("cpu") if req.get("cpu") else "N/A"
                        mem_lim_k8s = lim.get("memory") if lim.get("memory") else "N/A"
                        cpu_lim_k8s = lim.get("cpu") if lim.get("cpu") else "N/A"

                        with pod_lock:
                            cluster_stats["pods_kept"] += 1

                        return {
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

                    except (KeyError, ValueError) as e:
                        with pod_lock:
                            cluster_stats["parse_errors"] += 1
                        add_debug_sample(
                            "parse_error",
                            cluster_name,
                            pod_name=pod_name,
                            namespace=namespace,
                            step=step_name,
                            detail=str(e),
                        )
                        if debug:
                            print(
                                f"DEBUG: Error processing pod {pod_name}: {e}",
                                file=sys.stderr,
                            )
                        return None

                # Build (pod_name, namespace, step, step_name) work items for all pod×step combos.
                work_items = [
                    (
                        pod_name,
                        namespace,
                        step,
                        f"step-{step}" if not step.startswith("step-") else step,
                    )
                    for step in steps
                    for pod_name, namespace in pods
                ]

                _pll_pods_eff = max(1, pll_pods)
                with ThreadPoolExecutor(max_workers=_pll_pods_eff) as pod_exe:
                    for exec_record in pod_exe.map(_process_pod_step, work_items):
                        if exec_record:
                            cluster_executions.append(exec_record)

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
    progress_data = {
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
                    f"kept={cs['pods_kept']} query_failures={cs['query_failures']} "
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
