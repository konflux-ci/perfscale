from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict

from arl_constants import (
    _MAX_ACTIVE_CLUSTERS_IN_SPINNER,
    _PROGRESS_IO_LOCK,
)


def prompt_confirmation(task_name, steps, source="extracted from YAML"):
    """Prompt user for confirmation before proceeding.

    Args:
        task_name: Task name to confirm
        steps: List of step names to confirm
        source: Source of the values (for display)

    Returns:
        bool: True if user confirms, False otherwise
    """
    print("\n" + "=" * 80, file=sys.stderr)
    print("CONFIRMATION: Task and Steps Configuration", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(f"Source: {source}", file=sys.stderr)
    print(f"Task Name: {task_name}", file=sys.stderr)
    print(f"Steps ({len(steps)}):", file=sys.stderr)
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    while True:
        # CRITICAL: Flush stderr before prompting to ensure any previous output is visible
        sys.stderr.flush()
        sys.stdout.flush()
        response = input("Proceed with these values? [y/N]: ").strip().lower()
        if response in ("y", "yes"):
            # Print newline after confirmation to ensure clean separation
            print("", file=sys.stderr)
            sys.stderr.flush()
            return True
        elif response in ("n", "no", ""):
            return False
        else:
            print("Please enter 'y' or 'n'", file=sys.stderr)


def extract_cluster_list(wrapper_path):
    """Extract list of clusters from wrapper script.

    Returns:
        list: List of cluster context names
    """
    try:
        with open(wrapper_path) as f:
            lines = f.readlines()

        # Extract CONTEXTS line (only non-commented lines, same logic as check_cluster_connectivity)
        contexts_str = None
        for line in lines:
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue
            # Check for CONTEXTS (not commented)
            if stripped.startswith("CONTEXTS="):
                # Extract value between quotes
                match = re.search(r'CONTEXTS="([^"]*)"', line)
                if match:
                    contexts_str = match.group(1).strip()
                    break

        if not contexts_str:
            # Try to get contexts from kubectl
            result = subprocess.run(
                ["kubectl", "config", "get-contexts", "-o", "name"],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minutes timeout for connectivity check
            )
            if result.returncode == 0:
                contexts = [c.strip() for c in result.stdout.strip().split("\n") if c.strip()]
            else:
                return []
        else:
            # Handle cases where CONTEXTS uses command substitution
            if "$(" in contexts_str:
                # Execute command substitution
                cmd_match = re.search(r"\$\(([^)]+)\)", contexts_str)
                if cmd_match:
                    cmd = cmd_match.group(1).strip()
                    if "kubectl config get-contexts" in cmd:
                        result = subprocess.run(
                            ["kubectl", "config", "get-contexts", "-o", "name"],
                            capture_output=True,
                            text=True,
                            timeout=120,  # 2 minutes timeout for connectivity check
                        )
                        if result.returncode == 0:
                            contexts = [
                                c.strip() for c in result.stdout.strip().split("\n") if c.strip()
                            ]
                        else:
                            # Fallback to default if command fails
                            fallback_match = re.search(r'echo\s+[\'"]([^\'"]+)[\'"]', contexts_str)
                            if fallback_match:
                                contexts = [fallback_match.group(1).strip()]
                            else:
                                return []
                    else:
                        return []
                else:
                    return []
            else:
                # Simple string value - split by space
                contexts = [c.strip() for c in contexts_str.split() if c.strip()]

        # Remove duplicates while preserving order
        # CRITICAL: Use dict.fromkeys() for guaranteed deduplication
        # This is more efficient and ensures no duplicates slip through
        unique_contexts = list(
            dict.fromkeys(ctx.strip() for ctx in contexts if ctx and ctx.strip())
        )

        return unique_contexts
    except Exception as e:
        print(f"Warning: Could not extract cluster list: {e}", file=sys.stderr)
        return []


def get_cluster_display_name(cluster_ctx):
    """Extract short cluster display name from full context string.

    This function extracts a user-friendly short name for display purposes only.
    The full context string should still be used for all cluster operations.

    Example:
        Input:  'default/api-stone-prd-rh01-pg1f-p1-openshiftapps-com:6443/smodak'
        Output: 'stone-prd-rh01'

    Uses same regex logic as wrapper_for_promql.sh: s#.*/api-([^-]+-[^-]+-[^-]+).*#\1#

    Args:
        cluster_ctx: Full cluster context string (e.g., 'default/api-stone-prd-rh01-...')

    Returns:
        str: Short cluster display name (e.g., 'stone-prd-rh01')
    """
    # Use same regex as wrapper_for_promql.sh: s#.*/api-([^-]+-[^-]+-[^-]+).*#\1#
    match = re.search(r"/api-([^-]+-[^-]+-[^-]+)", cluster_ctx)
    if match:
        return match.group(1)
    # Fallback: try to extract from context name
    if "/" in cluster_ctx:
        parts = cluster_ctx.split("/")
        if len(parts) > 1:
            # Try to extract from parts
            for part in parts:
                if "api-" in part:
                    match = re.search(r"api-([^-]+-[^-]+-[^-]+)", part)
                    if match:
                        return match.group(1)
    # Last resort: return last part after /
    return cluster_ctx.split("/")[-1] if "/" in cluster_ctx else cluster_ctx


def _terminal_width():
    """Best-effort terminal width for truncating in-place progress lines."""
    try:
        return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
    except Exception:
        return 80


def _truncate_progress_line(message):
    """Fit message on one terminal row so \\r can overwrite it cleanly."""
    max_len = max(20, _terminal_width() - 1)
    if len(message) <= max_len:
        return message
    return message[: max_len - 1] + "…"


def _progress_overwrite(message):
    """Update the current progress line in place (no newline)."""
    message = _truncate_progress_line(message)
    with _PROGRESS_IO_LOCK:
        sys.stderr.write(f"\r\033[K{message}")
        sys.stderr.flush()


def _progress_milestone(message):
    """Print a one-line milestone (e.g. cluster checkpoint), then free the line for the spinner.

    Clears any in-progress spinner row first so \\r overwrite cannot leave wrapped junk.
    """
    with _PROGRESS_IO_LOCK:
        sys.stderr.write(f"\r\033[K{message}\n")
        sys.stderr.flush()


def _spinner_thread(stop_event, progress_data=None, progress_lock=None, total_clusters=0):
    """Display a spinning wheel with percentage progress while collecting data from clusters.

    Shows overall cluster completion percentage plus a live pod-progress counter for each
    cluster that is currently being processed, so operators can distinguish a slow cluster
    from a truly stuck one.

    The status line is overwritten in place (\\\\r). Milestone events (checkpoints) print on
    their own line via _progress_milestone so they do not break the spinner.
    """
    spinner_chars = ["|", "/", "-", "\\"]
    idx = 0
    while not stop_event.is_set():
        percentage = 0
        completed_count = 0
        pods_listed = 0
        pods_kept = 0
        active_parts = []
        if progress_data and total_clusters > 0 and progress_lock:
            with progress_lock:
                completed_count = len(progress_data.get("completed", []))
                done_set = set(progress_data.get("completed", []))
                pods_listed = progress_data.get("pods_listed", 0)
                pods_kept = progress_data.get("pods_kept", 0)
                # Build per-cluster pod progress from live stats references
                for cname, info in progress_data.get("active_clusters", {}).items():
                    if cname in done_set:
                        continue
                    # stats_ref is the cluster worker's own stats dict (shared reference,
                    # no lock needed for reading — GIL makes individual dict reads safe here)
                    done_n = info["stats_ref"].get("pods_queried", 0)
                    total_n = info["total"]
                    active_parts.append(f"{cname}:{done_n}/{total_n}")
            percentage = int((completed_count / total_clusters) * 100)
        spin = spinner_chars[idx % len(spinner_chars)]
        # Cap how many active clusters we show so the line stays short enough to overwrite.
        if active_parts:
            shown = active_parts[:_MAX_ACTIVE_CLUSTERS_IN_SPINNER]
            extra = len(active_parts) - len(shown)
            active_body = "  ".join(shown)
            if extra > 0:
                active_body += f" +{extra} more"
            active_str = f"  active→[{active_body}]"
        else:
            active_str = ""
        if percentage > 0:
            message = (
                f"Clusters: {percentage}% ({completed_count}/{total_clusters} done)"
                f"{active_str}"
                f"  [listed={pods_listed} kept={pods_kept}] {spin}"
            )
        else:
            message = (
                f"Clusters: starting...{active_str}  [listed={pods_listed} kept={pods_kept}] {spin}"
            )
        _progress_overwrite(message)
        idx += 1
        time.sleep(0.2)
    with _PROGRESS_IO_LOCK:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def format_promql_duration(seconds):
    """Format a lookback window for PromQL range selectors (e.g. 1d, 6h, 90m)."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def format_lookback_label(days, hours):
    """Human-readable lookback like '7d', '6h', or '1d+6h'."""
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    return "+".join(parts) if parts else "0"


def resolve_lookback_seconds(days, hours):
    """Combine --days and --hours into a total lookback in seconds."""
    days = int(days or 0)
    hours = int(hours or 0)
    if days < 0 or hours < 0:
        raise ValueError("--days and --hours must be >= 0")
    total = days * 86400 + hours * 3600
    if total <= 0:
        raise ValueError("Lookback window must be > 0 (use --days and/or --hours)")
    return total


def _empty_collection_counters():
    return {
        "pods_listed": 0,
        "pods_queried": 0,
        "pods_kept": 0,
        "query_failures": 0,
        "empty_metrics": 0,
        "parse_errors": 0,
        "list_failures": 0,
    }


def _merge_counters(dest, src):
    for key, value in src.items():
        dest[key] = dest.get(key, 0) + value


def parse_csv_data(csv_text):
    """Parse CSV data (same format as wrapper script or detailed_executions_to_csv output)."""
    data = []
    lines = [line for line in csv_text.strip().split("\n") if line.strip()]
    if not lines:
        return data

    # Check if we only have header line (no data rows)
    if len(lines) <= 1:
        return data

    reader = csv.DictReader(lines)
    for row in reader:
        # Clean up keys (remove spaces and quotes)
        cleaned_row = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items()}
        data.append(cleaned_row)
    return data


def round_memory_to_standard(mb):
    """Round memory to standard Kubernetes values.

    For values < 1Gi: round to increments of 64Mi (64Mi, 128Mi, 192Mi, 256Mi, etc.)
    For values >= 1Gi: round to whole Gi values (1Gi, 2Gi, 3Gi, etc.)

    Minimum value: 64Mi (allows fine granularity while being more standard than 32Mi)
    Always rounds up to ensure we don't go below the recommended value.
    """
    mb = float(mb)

    # Enforce minimum of 64Mi (allows fine granularity while being more standard than 32Mi)
    MIN_MEMORY_MB = 64

    if mb < MIN_MEMORY_MB:
        mb = MIN_MEMORY_MB

    if mb < 1024:
        # Round UP to next highest increment of 64Mi
        # Using +63 ensures we always round up (e.g., 65Mi -> 128Mi, not 64Mi)
        rounded = ((int(mb) + 63) // 64) * 64

        # Ensure minimum
        if rounded < MIN_MEMORY_MB:
            rounded = MIN_MEMORY_MB

        return rounded
    else:
        # Round to nearest whole Gi, but round up if fractional
        gi = mb / 1024.0
        rounded_gi = round(gi)
        # If rounding down would go below original, round up instead
        if rounded_gi * 1024 < mb:
            rounded_gi += 1
        return rounded_gi * 1024


def mb_to_kubernetes(mb):
    """Convert MB to Kubernetes memory format with standard rounding."""
    mb = float(mb)
    rounded_mb = round_memory_to_standard(mb)

    if rounded_mb < 1024:
        return f"{int(rounded_mb)}Mi"
    else:
        gi = rounded_mb / 1024.0
        # Should always be whole number after rounding
        return f"{int(gi)}Gi"


def round_cpu_to_standard(cores):
    """Round CPU to standard Kubernetes values.

    Rounds UP to next highest increment of 50m (50m, 100m, 150m, 200m, etc.)
    Minimum value: 50m (allows finer granularity)
    Always rounds up to ensure we don't go below the recommended value.
    """
    cores = float(cores)
    millicores = cores * 1000

    # Enforce minimum of 50m (allows finer granularity)
    MIN_CPU_MILLICORES = 50

    if millicores < MIN_CPU_MILLICORES:
        millicores = MIN_CPU_MILLICORES

    # Round UP to next highest increment of 50m
    # Using +49 ensures we always round up (e.g., 51m -> 100m, not 50m)
    rounded_m = ((int(millicores) + 49) // 50) * 50

    # Ensure minimum
    if rounded_m < MIN_CPU_MILLICORES:
        rounded_m = MIN_CPU_MILLICORES

    return rounded_m / 1000.0


def cores_to_kubernetes(cores):
    """Convert cores to Kubernetes CPU format, always in millicores."""
    cores = float(cores)
    rounded_cores = round_cpu_to_standard(cores)

    # Always return as millicores
    millicores = int(rounded_cores * 1000)
    return f"{millicores}m"


def parse_cpu_value(cpu_str):
    """Parse CPU value from format like '3569m' or '4.5'."""
    if not cpu_str or cpu_str == "0m" or cpu_str == "0":
        return 0.0
    if cpu_str.endswith("m"):
        return float(cpu_str[:-1]) / 1000.0
    return float(cpu_str)


def _percentile(sorted_values, p):
    """Return the value at percentile p (0..1) from a sorted list. Empty -> 0."""
    if not sorted_values:
        return 0
    idx = int((len(sorted_values) - 1) * p)
    idx = max(0, min(idx, len(sorted_values) - 1))
    return sorted_values[idx]


def detailed_executions_to_csv(executions):
    """Build main pipeline CSV from detailed per-pod executions (single source of truth).

    Groups by (cluster, task, step); computes max, p95, p90, median for memory and CPU;
    outputs one row per group in the same format as the wrapper CSV for parse_csv_data.
    Step names in output are without 'step-' prefix (e.g. prefetch-dependencies).

    Returns:
        str: CSV string with header and data rows, or empty string if no executions.
    """
    if not executions:
        return ""
    header = (
        '"cluster", "task", "step", "pod_max_mem", '
        '"namespace_max_mem", "component_max_mem", "application_max_mem", '
        '"mem_max_mb", "mem_p95_mb", "mem_p90_mb", "mem_median_mb", '
        '"pod_max_cpu", "namespace_max_cpu", "component_max_cpu", "application_max_cpu", '
        '"cpu_max", "cpu_p95", "cpu_p90", "cpu_median"'
    )
    by_key = defaultdict(list)
    for e in executions:
        cluster = e.get("cluster", "")
        task = e.get("task", "")
        step = e.get("step", "")  # already without step- prefix
        if not step:
            continue
        key = (cluster, task, step)
        by_key[key].append(e)
    rows = []
    for cluster, task, step in sorted(by_key.keys()):
        group = by_key[(cluster, task, step)]
        mem_vals = sorted([float(x.get("memory_mb", 0)) for x in group])
        cpu_vals = sorted([float(x.get("cpu_cores", 0)) for x in group])
        mem_max = max(mem_vals) if mem_vals else 0
        mem_p95 = _percentile(mem_vals, 0.95)
        mem_p90 = _percentile(mem_vals, 0.90)
        mem_median = _percentile(mem_vals, 0.50)
        cpu_max = max(cpu_vals) if cpu_vals else 0
        cpu_p95 = _percentile(cpu_vals, 0.95)
        cpu_p90 = _percentile(cpu_vals, 0.90)
        cpu_median = _percentile(cpu_vals, 0.50)
        # Pod/namespace/component/application for max mem
        max_mem_exec = max(group, key=lambda x: float(x.get("memory_mb", 0)))
        pod_max_mem = max_mem_exec.get("pod", "")
        namespace_max_mem = max_mem_exec.get("namespace", "")
        component_max_mem = max_mem_exec.get("component", "N/A")
        application_max_mem = max_mem_exec.get("application", "N/A")
        # Pod/namespace/component/application for max cpu
        max_cpu_exec = max(group, key=lambda x: float(x.get("cpu_cores", 0)))
        pod_max_cpu = max_cpu_exec.get("pod", "")
        namespace_max_cpu = max_cpu_exec.get("namespace", "")
        component_max_cpu = max_cpu_exec.get("component", "N/A")
        application_max_cpu = max_cpu_exec.get("application", "N/A")
        cpu_max_m = f"{int(round(cpu_max * 1000))}m"
        cpu_p95_m = f"{int(round(cpu_p95 * 1000))}m"
        cpu_p90_m = f"{int(round(cpu_p90 * 1000))}m"
        cpu_median_m = f"{int(round(cpu_median * 1000))}m"
        row = (
            f'"{cluster}", "{task}", "{step}", '
            f'"{pod_max_mem}", "{namespace_max_mem}", '
            f'"{component_max_mem}", "{application_max_mem}", '
            f'"{int(round(mem_max))}", "{int(round(mem_p95))}", '
            f'"{int(round(mem_p90))}", "{int(round(mem_median))}", '
            f'"{pod_max_cpu}", "{namespace_max_cpu}", '
            f'"{component_max_cpu}", "{application_max_cpu}", '
            f'"{cpu_max_m}", "{cpu_p95_m}", "{cpu_p90_m}", "{cpu_median_m}"'
        )
        rows.append(row)
    return header + "\n" + "\n".join(rows)


def _parse_cpu_millicores_for_verify(s):
    """Parse CPU from main CSV e.g. '1194m' -> 1194."""
    s = (s or "").strip().rstrip("m")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def verify_aggregates_against_detailed(detailed_executions, aggregated_rows):
    """Verify aggregated rows match recomputation from detailed executions.

    Used for single-source sanity check: the main table is derived
    from the same executions as the detailed files; this recomputes
    per (cluster, step) from executions and compares.

    Args:
        detailed_executions: List of execution dicts
            (cluster, step, memory_mb, cpu_cores, ...)
        aggregated_rows: List of parsed CSV rows (from parse_csv_data)
            with mem_max_mb, mem_p95_mb, etc.

    Returns:
        tuple: (all_ok: bool, messages: list of str)
    """
    if not detailed_executions or not aggregated_rows:
        return True, []
    by_key = defaultdict(list)
    for e in detailed_executions:
        cluster = e.get("cluster", "")
        step = e.get("step", "")
        if not step:
            continue
        key = (cluster, step)
        by_key[key].append(e)
    main_by_key = {}
    for row in aggregated_rows:
        cluster = (row.get("cluster") or "").strip()
        step = (row.get("step") or "").strip()
        if not cluster or not step:
            continue
        key = (cluster, step)
        main_by_key[key] = {
            "mem_max_mb": int(float(row.get("mem_max_mb") or 0)),
            "mem_p95_mb": int(float(row.get("mem_p95_mb") or 0)),
            "mem_p90_mb": int(float(row.get("mem_p90_mb") or 0)),
            "mem_median_mb": int(float(row.get("mem_median_mb") or 0)),
            "cpu_max_m": _parse_cpu_millicores_for_verify(row.get("cpu_max")),
            "cpu_p95_m": _parse_cpu_millicores_for_verify(row.get("cpu_p95")),
            "cpu_p90_m": _parse_cpu_millicores_for_verify(row.get("cpu_p90")),
            "cpu_median_m": _parse_cpu_millicores_for_verify(row.get("cpu_median")),
        }
    messages = []
    all_ok = True
    for key in sorted(by_key.keys()):
        cluster, step = key
        group = by_key[key]
        mem_vals = sorted([float(x.get("memory_mb", 0)) for x in group])
        cpu_vals = sorted([float(x.get("cpu_cores", 0)) for x in group])
        comp = {
            "mem_max_mb": int(round(max(mem_vals))) if mem_vals else 0,
            "mem_p95_mb": int(round(_percentile(mem_vals, 0.95))),
            "mem_p90_mb": int(round(_percentile(mem_vals, 0.90))),
            "mem_median_mb": int(round(_percentile(mem_vals, 0.50))),
            "cpu_max_m": int(round(max(cpu_vals) * 1000)) if cpu_vals else 0,
            "cpu_p95_m": int(round(_percentile(cpu_vals, 0.95) * 1000)),
            "cpu_p90_m": int(round(_percentile(cpu_vals, 0.90) * 1000)),
            "cpu_median_m": int(round(_percentile(cpu_vals, 0.50) * 1000)),
        }
        main_vals = main_by_key.get(key)
        if not main_vals:
            messages.append(f"Verify: main has no row for cluster={cluster} step={step}")
            all_ok = False
            continue
        mismatches = []
        for k in comp:
            if main_vals[k] != comp[k]:
                mismatches.append(f"{k}: main={main_vals[k]} recomputed={comp[k]}")
        if mismatches:
            all_ok = False
            messages.append(
                f"Verify MISMATCH cluster={cluster} step={step} (n={len(group)} pods): "
                + "; ".join(mismatches)
            )
    return all_ok, messages


def analyze_step_data(step_name, step_rows, margin_pct=10, base="max"):
    """Analyze data for a specific step and return recommendations.

    Args:
        step_name: Name of the step
        step_rows: List of data rows for this step
        margin_pct: Safety margin percentage to add
        base: Base metric to use ('max', 'p95', 'p90', 'median')
    """
    if not step_rows:
        return None

    # Extract memory values
    mem_max_values = [
        float(r["mem_max_mb"])
        for r in step_rows
        if r.get("mem_max_mb") and r["mem_max_mb"] not in ("0", "", "N/A")
    ]
    mem_p95_values = [
        float(r["mem_p95_mb"])
        for r in step_rows
        if r.get("mem_p95_mb") and r["mem_p95_mb"] not in ("0", "", "N/A")
    ]
    mem_p90_values = [
        float(r["mem_p90_mb"])
        for r in step_rows
        if r.get("mem_p90_mb") and r["mem_p90_mb"] not in ("0", "", "N/A")
    ]
    mem_median_values = [
        float(r["mem_median_mb"])
        for r in step_rows
        if r.get("mem_median_mb") and r["mem_median_mb"] not in ("0", "", "N/A")
    ]

    # Extract CPU values
    cpu_max_values = [
        parse_cpu_value(r.get("cpu_max", "0m")) for r in step_rows if r.get("cpu_max")
    ]
    cpu_p95_values = [
        parse_cpu_value(r.get("cpu_p95", "0m")) for r in step_rows if r.get("cpu_p95")
    ]
    cpu_p90_values = [
        parse_cpu_value(r.get("cpu_p90", "0m")) for r in step_rows if r.get("cpu_p90")
    ]
    cpu_median_values = [
        parse_cpu_value(r.get("cpu_median", "0m")) for r in step_rows if r.get("cpu_median")
    ]

    if not mem_max_values:
        return None

    # Calculate max across all clusters
    mem_max_max = max(mem_max_values)
    mem_p95_max = max(mem_p95_values) if mem_p95_values else 0
    mem_p90_max = max(mem_p90_values) if mem_p90_values else 0
    mem_median_max = max(mem_median_values) if mem_median_values else 0

    cpu_max_max = max(cpu_max_values) if cpu_max_values else 0
    cpu_p95_max = max(cpu_p95_values) if cpu_p95_values else 0
    cpu_p90_max = max(cpu_p90_values) if cpu_p90_values else 0
    cpu_median_max = max(cpu_median_values) if cpu_median_values else 0

    # Select base value based on user choice
    if base == "max":
        mem_base = mem_max_max
        cpu_base = cpu_max_max
        base_label = "Max"
    elif base == "p95":
        mem_base = mem_p95_max if mem_p95_max > 0 else mem_max_max
        cpu_base = cpu_p95_max if cpu_p95_max > 0 else cpu_max_max
        base_label = "P95"
    elif base == "p90":
        mem_base = mem_p90_max if mem_p90_max > 0 else mem_max_max
        cpu_base = cpu_p90_max if cpu_p90_max > 0 else cpu_max_max
        base_label = "P90"
    elif base == "median":
        mem_base = mem_median_max if mem_median_max > 0 else mem_max_max
        cpu_base = cpu_median_max if cpu_median_max > 0 else cpu_max_max
        base_label = "Median"
    else:
        # Default to max if invalid option
        mem_base = mem_max_max
        cpu_base = cpu_max_max
        base_label = "Max"

    # Calculate recommendations: base + margin, but don't exceed max observed
    mem_recommended = (
        min(mem_max_max, int(mem_base * (1 + margin_pct / 100))) if mem_base > 0 else mem_max_max
    )
    if cpu_base > 0:
        cpu_recommended = min(cpu_max_max * 1.1, cpu_base * (1 + margin_pct / 100))
    else:
        cpu_recommended = cpu_max_max if cpu_max_max > 0 else 0

    # Count coverage
    mem_coverage = len([x for x in mem_max_values if x <= mem_recommended])
    cpu_coverage = len([x for x in cpu_max_values if x <= cpu_recommended]) if cpu_max_values else 0

    return {
        "step_name": step_name,
        "mem_recommended_mb": mem_recommended,
        "mem_recommended_k8s": mb_to_kubernetes(mem_recommended),
        "cpu_recommended_cores": cpu_recommended,
        "cpu_recommended_k8s": cores_to_kubernetes(cpu_recommended),
        "mem_max_max": mem_max_max,
        "mem_p95_max": mem_p95_max,
        "mem_p90_max": mem_p90_max,
        "mem_median_max": mem_median_max,
        "mem_base": mem_base,
        "cpu_max_max": cpu_max_max,
        "cpu_p95_max": cpu_p95_max,
        "cpu_p90_max": cpu_p90_max,
        "cpu_median_max": cpu_median_max,
        "cpu_base": cpu_base,
        "base_label": base_label,
        "mem_coverage": mem_coverage,
        "mem_total": len(mem_max_values),
        "cpu_coverage": cpu_coverage,
        "cpu_total": len(cpu_max_values) if cpu_max_values else 0,
    }
