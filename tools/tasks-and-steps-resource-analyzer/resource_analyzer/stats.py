"""Aggregation, recommendations, and verification helpers."""

import csv
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

from .task_yaml import normalize_step_name_for_compare


def parse_csv_data(csv_text) -> Any:
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


def round_memory_to_standard(mb) -> Any:
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


def mb_to_kubernetes(mb) -> Any:
    """Convert MB to Kubernetes memory format with standard rounding."""
    mb = float(mb)
    rounded_mb = round_memory_to_standard(mb)

    if rounded_mb < 1024:
        return f"{int(rounded_mb)}Mi"
    else:
        gi = rounded_mb / 1024.0
        # Should always be whole number after rounding
        return f"{int(gi)}Gi"


def round_cpu_to_standard(cores) -> Any:
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


def cores_to_kubernetes(cores) -> Any:
    """Convert cores to Kubernetes CPU format, always in millicores."""
    cores = float(cores)
    rounded_cores = round_cpu_to_standard(cores)

    # Always return as millicores
    millicores = int(rounded_cores * 1000)
    return f"{millicores}m"


def parse_cpu_value(cpu_str) -> Any:
    """Parse CPU value from format like '3569m' or '4.5'."""
    if not cpu_str or cpu_str == "0m" or cpu_str == "0":
        return 0.0
    if cpu_str.endswith("m"):
        return float(cpu_str[:-1]) / 1000.0
    return float(cpu_str)


def _percentile(sorted_values, p) -> Any:
    """Return the value at percentile p (0..1) from a sorted list. Empty -> 0."""
    if not sorted_values:
        return 0
    idx = int((len(sorted_values) - 1) * p)
    idx = max(0, min(idx, len(sorted_values) - 1))
    return sorted_values[idx]


def detailed_executions_to_csv(executions) -> Any:
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


def _parse_cpu_millicores_for_verify(s) -> Any:
    """Parse CPU from main CSV e.g. '1194m' -> 1194."""
    s = (s or "").strip().rstrip("m")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def verify_aggregates_against_detailed(detailed_executions, aggregated_rows) -> Any:
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


def analyze_step_data(step_name, step_rows, margin_pct=10, base="max") -> Any:
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


def analyze_step_data_all_bases(step_name, step_rows, margin_pct=5) -> Any:
    """Analyze data for a specific step and return recommendations for all base metrics.

    Args:
        step_name: Name of the step
        step_rows: List of data rows for this step
        margin_pct: Safety margin percentage to add (applied to all base metrics)

    Returns:
        Dictionary with recommendations for all base metrics: {'max': {...}, 'p95': {...}, 'p90':
        {...}, 'median': {...}}
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

    # Generate recommendations for all base metrics
    all_recommendations = {}

    for base in ["max", "p95", "p90", "median"]:
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

        # Calculate recommendations: base + margin, but don't exceed max observed
        mem_recommended = (
            min(mem_max_max, int(mem_base * (1 + margin_pct / 100)))
            if mem_base > 0
            else mem_max_max
        )
        if cpu_base > 0:
            cpu_recommended = min(cpu_max_max * 1.1, cpu_base * (1 + margin_pct / 100))
        else:
            cpu_recommended = cpu_max_max if cpu_max_max > 0 else 0

        # Count coverage
        mem_coverage = len([x for x in mem_max_values if x <= mem_recommended])
        cpu_coverage = (
            len([x for x in cpu_max_values if x <= cpu_recommended]) if cpu_max_values else 0
        )

        all_recommendations[base] = {
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

    return all_recommendations


def print_comparison_table(
    recommendations, current_resources=None, task_name=None, save_html=True
) -> Any:
    """Print comparison table of current vs proposed resource limits.

    Also saves the comparison table as HTML if task_name is provided and save_html is True.

    Args:
        recommendations: List of recommendation dictionaries
        current_resources: Dictionary of current resources by step name
        task_name: Optional task name for HTML file generation
        save_html: Whether to save HTML file (default: True). Set to False in Phase 1 to avoid
        duplicate files.

    Returns:
        Path to saved HTML file if task_name provided and save_html is True, None otherwise
    """
    if not recommendations:
        return None

    print("=" * 100)
    print("RESOURCE LIMITS COMPARISON: CURRENT vs PROPOSED")
    print("=" * 100)
    print()

    # Table header with reduced spacing
    print(
        f"{'Step':<15} {'Current Requests':<20} {'Proposed Requests':<20} "
        f"{'Current Limits':<20} {'Proposed Limits':<20}"
    )
    print("-" * 100)

    for rec in recommendations:
        if rec is None:
            continue

        step_name = rec["step_name"]
        # Convert step-build to build for matching
        step_name_yaml = normalize_step_name_for_compare(step_name)
        proposed_mem = rec["mem_recommended_k8s"]
        proposed_cpu = rec["cpu_recommended_k8s"]

        # Get current values
        if current_resources and step_name_yaml in current_resources:
            curr = current_resources[step_name_yaml]
            curr_mem_req = curr["requests"].get("memory") or "null"
            curr_cpu_req = curr["requests"].get("cpu") or "null"
            curr_mem_lim = curr["limits"].get("memory") or "null"
            curr_cpu_lim = curr["limits"].get("cpu") or "null"
        else:
            curr_mem_req = "N/A"
            curr_cpu_req = "N/A"
            curr_mem_lim = "N/A"
            curr_cpu_lim = "N/A"

        # Format values (memory / cpu)
        curr_req = f"{curr_mem_req} / {curr_cpu_req}"
        curr_lim = f"{curr_mem_lim} / {curr_cpu_lim}"
        prop_req = f"{proposed_mem} / {proposed_cpu}"
        prop_lim = f"{proposed_mem} / {proposed_cpu}"

        print(f"{step_name_yaml:<15} {curr_req:<20} {prop_req:<20} {curr_lim:<20} {prop_lim:<20}")

    print()

    # Save as HTML if task_name provided and save_html is True
    comparison_html_path = None
    if task_name and save_html:
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        from .reporting import save_comparison_table_to_html

        comparison_html_path = save_comparison_table_to_html(
            recommendations, current_resources, task_name, timestamp_str
        )
        if comparison_html_path:
            print(
                f"Saved comparison table as HTML: {comparison_html_path}",
                file=sys.stderr,
            )

    return comparison_html_path


def print_analysis(
    recommendations,
    margin_pct,
    base="max",
    current_resources=None,
    task_name=None,
    save_comparison_html=True,
) -> Any:
    """Print analysis results.

    Args:
        recommendations: List of recommendation dictionaries
        margin_pct: Margin percentage used
        base: Base metric used
        current_resources: Optional dictionary of current resources
        task_name: Optional task name for HTML file generation
        save_comparison_html: Whether to save comparison HTML file (default: True). Set to False in
        Phase 1 to avoid duplicate files.

    Returns:
        Path to comparison HTML file if saved, None otherwise
    """
    base_label = (
        recommendations[0]["base_label"] if recommendations and recommendations[0] else base.upper()
    )
    print("=" * 80)
    print(f"RESOURCE LIMIT RECOMMENDATIONS ({base_label} + {margin_pct}% Safety Margin)")
    print("=" * 80)
    print()

    for rec in recommendations:
        if rec is None:
            continue

        print(f"Step: {rec['step_name']}")
        print("-" * 80)
        print(f"  Memory: {rec['mem_recommended_k8s']}")
        if rec["base_label"] == "Max":
            print(f"    - Base ({rec['base_label']}): {mb_to_kubernetes(rec['mem_base'])}")
        else:
            print(f"    - Base ({rec['base_label']}): {mb_to_kubernetes(rec['mem_base'])}")
            print(f"    - Max observed: {mb_to_kubernetes(rec['mem_max_max'])}")
        print(f"    - Coverage: {rec['mem_coverage']}/{rec['mem_total']} clusters")
        print()

        if rec["cpu_total"] > 0:
            print(f"  CPU: {rec['cpu_recommended_k8s']}")
            if rec["base_label"] == "Max":
                print(f"    - Base ({rec['base_label']}): {cores_to_kubernetes(rec['cpu_base'])}")
            else:
                print(f"    - Base ({rec['base_label']}): {cores_to_kubernetes(rec['cpu_base'])}")
                print(f"    - Max observed: {cores_to_kubernetes(rec['cpu_max_max'])}")
            print(f"    - Coverage: {rec['cpu_coverage']}/{rec['cpu_total']} clusters")
        else:
            print("  CPU: No data available")
        print()

    # Print comparison table if current resources are available
    comparison_html_path = None
    if current_resources:
        print()
        comparison_html_path = print_comparison_table(
            recommendations,
            current_resources,
            task_name,
            save_html=save_comparison_html,
        )

    return comparison_html_path
