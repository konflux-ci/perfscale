"""HTML/JSON/CSV report writers, cache paths, and report banners."""

import csv
import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

from .paths import TOOL_DIR
from .progress import _progress_milestone
from .stats import (
    mb_to_kubernetes,
)
from .task_yaml import normalize_step_name_for_compare


def _html_steps_missing_observability_banner(steps_missing) -> Any:
    """HTML notice when YAML declares steps that did not appear in aggregated metrics."""
    if not steps_missing:
        return ""
    items = "\n".join(f"        <li>{html.escape(s)}</li>" for s in steps_missing)
    return (
        '    <div style="background:#fff3cd;border:1px solid #ffc107;'
        'padding:12px 16px;margin:16px 0;border-radius:4px;">\n'
        "        <strong>Steps in YAML with no observability data in this run</strong>\n"
        f'        <ul style="margin:8px 0 0 16px;">{items}\n        </ul>\n'
        '        <p style="margin:8px 0 0 0;font-size:0.95em;color:#555;">'
        "Recommendations only include steps with Prometheus samples for "
        "<code>container=&quot;step-&lt;name&gt;&quot;</code> in the analysis window.</p>\n"
        "    </div>\n"
    )


# ---------------------------------------------------------------------------
# Finding 2: Cluster data coverage window
# ---------------------------------------------------------------------------


def compute_cluster_coverage_report(detailed_executions, days_requested) -> Any:
    """Compute the actual data coverage window per cluster from collected executions.

    Returns a dict keyed by cluster display name:
        {cluster: {'oldest_date': str, 'days_covered': float,
                   'meets_requested': bool, 'pod_count': int}}
    """
    if not detailed_executions:
        return {}

    now = datetime.now()
    by_cluster = defaultdict(list)
    for e in detailed_executions:
        cluster = e.get("cluster", "unknown")
        ts = e.get("timestamp", "")
        if ts:
            by_cluster[cluster].append(ts)

    report = {}
    for cluster, timestamps in by_cluster.items():
        valid_ts = []
        for ts in timestamps:
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                valid_ts.append(dt)
            except (ValueError, TypeError):
                pass
        if not valid_ts:
            continue
        oldest = min(valid_ts)
        days_covered = (now - oldest).total_seconds() / 86400.0
        report[cluster] = {
            "oldest_date": oldest.strftime("%Y-%m-%d %H:%M:%S"),
            "days_covered": round(days_covered, 1),
            "meets_requested": days_covered >= (days_requested * 0.9),  # 10% tolerance
            "pod_count": len(timestamps),
        }
    return report


def _html_cluster_coverage_banner(coverage_report, days_requested) -> Any:
    """HTML banner showing actual data coverage per cluster."""
    if not coverage_report:
        return ""

    rows = ""
    has_warning = False
    for cluster in sorted(coverage_report.keys()):
        info = coverage_report[cluster]
        days = info["days_covered"]
        meets = info["meets_requested"]
        oldest = info["oldest_date"]
        pods = info["pod_count"]
        if not meets:
            has_warning = True
            icon = "&#9888;"  # warning triangle
            row_style = "color:#856404;background:#fff3cd;"
        else:
            icon = "&#10003;"  # check mark
            row_style = "color:#155724;"
        rows += (
            f'<tr style="{row_style}">'
            f'<td style="padding:4px 10px;">{html.escape(cluster)}</td>'
            f'<td style="padding:4px 10px;">{icon} {days:.1f} days</td>'
            f'<td style="padding:4px 10px;">{oldest}</td>'
            f'<td style="padding:4px 10px;">{pods}</td>'
            f"</tr>\n"
        )

    border_color = "#ffc107" if has_warning else "#28a745"
    bg_color = "#fff3cd" if has_warning else "#d4edda"
    heading = (
        f"&#9888; Some clusters returned less data than the requested {days_requested} days — "
        "statistics may under-represent rare heavy workloads on those clusters."
        if has_warning
        else f"&#10003; All clusters returned data covering the full {days_requested}-day window."
    )

    return (
        f'    <div style="background:{bg_color};border:1px solid {border_color};padding:12px 16px;'
        f'margin:16px 0;border-radius:4px;">\n'
        f"        <strong>Cluster Data Coverage Report</strong>"
        f" (requested: {days_requested} days)<br>\n"
        f'        <em style="font-size:0.9em;color:#555;">{heading}</em>\n'
        f'        <table style="margin-top:8px;border-collapse:collapse;font-size:0.9em;">\n'
        f'          <tr style="font-weight:bold;">'
        f'<td style="padding:4px 10px;">Cluster</td>'
        f'<td style="padding:4px 10px;">Coverage</td>'
        f'<td style="padding:4px 10px;">Oldest Data Point</td>'
        f'<td style="padding:4px 10px;">Pod Executions</td></tr>\n'
        f"{rows}"
        f"        </table>\n"
        f"    </div>\n"
    )


# ---------------------------------------------------------------------------
# Finding 3: Scrape-interval transparency and heavy-tail warnings
# ---------------------------------------------------------------------------


def _html_scrape_interval_note() -> Any:
    """HTML advisory about Prometheus scrape interval and sub-scrape memory spikes."""
    return (
        '    <div style="background:#e8f4fd;border:1px solid #90caf9;padding:12px 16px;'
        'margin:16px 0;border-radius:4px;">\n'
        "        <strong>&#8505; Prometheus Scrape Interval Notice</strong>\n"
        '        <p style="margin:8px 0 0 0;'
        'font-size:0.95em;color:#333;">'
        "Memory values are the <em>maximum of all scraped"
        " samples</em> within the analysis window. "
        "The Prometheus scrape interval for "
        "<code>container_memory_working_set_bytes</code>"
        " is typically <strong>15&ndash;30 seconds"
        "</strong> on Konflux clusters. A transient "
        "memory spike that resolves faster than one "
        "scrape interval (e.g. a brief image-"
        "decompression burst) will <strong>not</strong>"
        " appear in the data. Steps that perform large,"
        " short-lived memory allocations (image pulls, "
        "decompression, GC) may have actual peak usage "
        "higher than reported. For long-term "
        "improvement, Splunk-archived metrics at higher "
        "resolution can be used to cross-check "
        "Prometheus data.</p>\n"
        "    </div>\n"
    )


def compute_heavy_tail_warnings(all_recommendations_by_base) -> Any:
    """Return per-step heavy-tail warnings when max/p95 ratio exceeds threshold.

    Returns a list of dicts:
        [{'step': str, 'mem_max_mb': float, 'mem_p95_mb': float, 'ratio': float,
          'p99_mb': float (if available), 'cpu_max': float, 'cpu_p95': float}]
    """
    RATIO_WARN = 3.0
    warnings = []
    max_recs = all_recommendations_by_base.get("max", [])
    p95_recs = all_recommendations_by_base.get("p95", [])

    p95_by_step = {r["step_name"]: r for r in p95_recs if r}
    for rec in max_recs:
        if not rec:
            continue
        step = rec["step_name"]
        mem_max = rec.get("mem_max_max", 0)
        p95_rec = p95_by_step.get(step)
        mem_p95 = p95_rec.get("mem_p95_max", 0) if p95_rec else rec.get("mem_p95_max", 0)
        cpu_max = rec.get("cpu_max_max", 0)
        cpu_p95 = p95_rec.get("cpu_p95_max", 0) if p95_rec else rec.get("cpu_p95_max", 0)
        if mem_p95 > 0 and mem_max / mem_p95 >= RATIO_WARN:
            warnings.append(
                {
                    "step": step,
                    "mem_max_mb": mem_max,
                    "mem_p95_mb": mem_p95,
                    "ratio": round(mem_max / mem_p95, 1),
                    "cpu_max_cores": cpu_max,
                    "cpu_p95_cores": cpu_p95,
                }
            )
    return warnings


def _html_heavy_tail_warnings_banner(warnings) -> Any:
    """HTML banner surfacing heavy-tail distribution warnings for reviewers."""
    if not warnings:
        return ""

    rows = ""
    for w in warnings:
        mem_max_k8s = mb_to_kubernetes(w["mem_max_mb"])
        mem_p95_k8s = mb_to_kubernetes(w["mem_p95_mb"])
        rows += (
            f"<tr>"
            f'<td style="padding:5px 10px;font-weight:bold;">'
            f"{html.escape(normalize_step_name_for_compare(w['step']))}"
            f"</td>"
            f'<td style="padding:5px 10px;">{mem_max_k8s} ({w["mem_max_mb"]:.0f} MB)</td>'
            f'<td style="padding:5px 10px;">{mem_p95_k8s} ({w["mem_p95_mb"]:.0f} MB)</td>'
            f'<td style="padding:5px 10px;color:#c62828;font-weight:bold;">{w["ratio"]}&#215;</td>'
            f"</tr>\n"
        )

    return (
        '    <div style="background:#fff8e1;border:2px solid #f9a825;padding:12px 16px;'
        'margin:16px 0;border-radius:4px;">\n'
        "        <strong>&#9888; Heavy-Tail Distribution Warning</strong>\n"
        '        <p style="margin:8px 0 4px 0;font-size:0.95em;color:#555;">'
        "The following step(s) show a Max/P95 memory ratio &ge; 3&times;. "
        "This means rare but heavy workloads (outlier tenants, large bundles) can require "
        "significantly more memory than the p95 recommendation covers. "
        "Consider using the <strong>MAX</strong> base or a per-tenant resource override "
        "for these steps if OOM failures occur on outlier workloads.</p>\n"
        '        <table style="border-collapse:collapse;font-size:0.9em;margin-top:8px;">\n'
        '          <tr style="font-weight:bold;background:#fef9c3;">'
        '<td style="padding:5px 10px;">Step</td>'
        '<td style="padding:5px 10px;">Max Observed</td>'
        '<td style="padding:5px 10px;">P95</td>'
        '<td style="padding:5px 10px;">Max/P95 Ratio</td></tr>\n'
        f"{rows}"
        "        </table>\n"
        "    </div>\n"
    )


def _compute_violators_for_step(detailed_executions, step_name, mem_base, cpu_base) -> Any:
    """Find pod executions that exceed a given memory or CPU base threshold for one step.

    Groups results as: namespace → application → list-of-component-dicts.

    Returns:
        dict  { namespace: { application: [ {component, cluster, mem_max, cpu_max,
                                              count, mem_viol, cpu_viol} ] } }
        or empty dict if there are no violations.
    """
    from collections import defaultdict as _dd

    # Only violations make sense when there is a non-zero base
    has_mem_base = mem_base and mem_base > 0
    has_cpu_base = cpu_base and cpu_base > 0
    if not has_mem_base and not has_cpu_base:
        return {}

    # Normalise the step name we look for (executions may carry it without 'step-' prefix)
    step_bare = step_name.removeprefix("step-") if step_name.startswith("step-") else step_name

    groups: dict[tuple[Any, ...], dict[str, list[float]]] = _dd(
        lambda: {"mem_vals": [], "cpu_vals": []}
    )
    for r in detailed_executions:
        r_step = r.get("step", "")
        r_step_bare = r_step.removeprefix("step-") if r_step.startswith("step-") else r_step
        if r_step_bare != step_bare:
            continue
        mem = float(r.get("memory_mb", 0) or 0)
        cpu = float(r.get("cpu_cores", 0) or 0)
        mem_viol = has_mem_base and mem > mem_base
        cpu_viol = has_cpu_base and cpu > cpu_base
        if not (mem_viol or cpu_viol):
            continue
        key = (
            r.get("namespace", "unknown"),
            r.get("application", "unknown"),
            r.get("component", "unknown"),
            r.get("cluster", "unknown"),
        )
        groups[key]["mem_vals"].append(mem)
        groups[key]["cpu_vals"].append(cpu)

    if not groups:
        return {}

    result: dict[Any, dict[Any, list[dict[str, Any]]]] = _dd(lambda: _dd(list))
    for (ns, app, comp, cluster), vals in groups.items():
        mem_max = max(vals["mem_vals"]) if vals["mem_vals"] else 0
        cpu_max = max(vals["cpu_vals"]) if vals["cpu_vals"] else 0
        result[ns][app].append(
            {
                "component": comp,
                "cluster": cluster,
                "mem_max": mem_max,
                "cpu_max": cpu_max,
                "count": len(vals["mem_vals"]),
                "mem_viol": has_mem_base and mem_max > mem_base,
                "cpu_viol": has_cpu_base and cpu_max > cpu_base,
            }
        )

    # Sort components within each app by descending peak memory
    for ns in result:
        for app in result[ns]:
            result[ns][app].sort(key=lambda x: -x["mem_max"])

    return result


def _html_violators_block(
    viol_by_ns, mem_base, cpu_base, base_label, step_display_name, _table_counter=None
) -> Any:
    """Render a sortable, collapsible HTML block listing executions that exceed a base threshold.

    Produces a flat table (no rowspan) with data-val attributes on the numeric columns so
    that the sortVT() JavaScript function can sort by Peak Mem, vs Base, Peak CPU, # Pods.
    Namespace and Application cells keep their coloured backgrounds for visual grouping.
    """
    if _table_counter is None:
        _table_counter = [0]
    if not viol_by_ns:
        return (
            f'<p class="no-violators">&#10003;&nbsp; No executions exceed the '
            f"<em>{base_label}</em> baseline for step "
            f"<strong>{html.escape(step_display_name)}</strong>.</p>\n"
        )

    def _fmt_mb(mb) -> Any:
        return f"{mb / 1024:.2f} Gi" if mb >= 1024 else f"{mb:.0f} Mi"

    def _fmt_cpu(c) -> Any:
        if c == 0:
            return "0m"
        return f"{int(c * 1000)}m" if c < 1 else f"{c:.3f}"

    mem_thr = _fmt_mb(mem_base) if (mem_base and mem_base > 0) else "—"
    cpu_thr = _fmt_cpu(cpu_base) if (cpu_base and cpu_base > 0) else "—"

    # Flatten nested dict → list of row dicts, sorted by peak mem desc
    flat_rows = []
    for ns in sorted(viol_by_ns):
        for app in sorted(viol_by_ns[ns]):
            for row in viol_by_ns[ns][app]:
                flat_rows.append(dict(row, namespace=ns, application=app))
    flat_rows.sort(key=lambda r: -r["mem_max"])

    _table_counter[0] += 1
    tid = f"viol_tbl_{_table_counter[0]}"

    rows_html = ""
    for row in flat_rows:
        mem_over = row["mem_max"] - mem_base if row["mem_viol"] else 0
        cpu_over = row["cpu_max"] - cpu_base if row["cpu_viol"] else 0
        mem_vs = f"+{_fmt_mb(mem_over)}&nbsp;&#9888;" if row["mem_viol"] else "&#10003;&nbsp;ok"
        cpu_vs = f"+{_fmt_cpu(cpu_over)}&nbsp;&#9888;" if row["cpu_viol"] else "&#10003;&nbsp;ok"
        mc = ' class="viol-cell"' if row["mem_viol"] else ""
        cc = ' class="viol-cell"' if row["cpu_viol"] else ""
        # data-val carries raw numeric value for JS sort:
        #   mem / cpu → MB or millicores; "ok" rows get -1 so they sort to bottom
        mem_dv = f"{row['mem_max']:.1f}"
        memov_dv = f"{mem_over:.1f}" if row["mem_viol"] else "-1"
        cpu_dv = f"{row['cpu_max'] * 1000:.1f}"
        cpuov_dv = f"{cpu_over * 1000:.1f}" if row["cpu_viol"] else "-1"
        rows_html += (
            f"<tr>"
            f'<td class="viol-ns-cell">{html.escape(row["namespace"])}</td>'
            f'<td class="viol-app-cell">{html.escape(row["application"])}</td>'
            f"<td>{html.escape(row['component'])}</td>"
            f'<td><span class="cluster-badge">{html.escape(row["cluster"])}</span></td>'
            f'<td{mc} data-val="{mem_dv}">{_fmt_mb(row["mem_max"])}</td>'
            f'<td{mc} data-val="{memov_dv}">{mem_vs}</td>'
            f'<td{cc} data-val="{cpu_dv}">{_fmt_cpu(row["cpu_max"])}</td>'
            f'<td{cc} data-val="{cpuov_dv}">{cpu_vs}</td>'
            f'<td data-val="{row["count"]}">{row["count"]}</td>'
            f"</tr>\n"
        )

    return f"""<details class="violators-block">
  <summary class="violators-summary">
    &#9888;&nbsp; <strong>{len(flat_rows)}&nbsp;group(s)</strong>
    exceed the <em>{base_label}</em> baseline
    ({mem_thr}&nbsp;mem&nbsp;/&nbsp;{cpu_thr}&nbsp;cpu) for step
    <strong>{html.escape(step_display_name)}</strong>&nbsp;&#9660;
  </summary>
  <div class="violators-body">
    <p class="sort-hint">Click amber column header to sort &nbsp;&#8597;</p>
    <table id="{tid}" class="violators-table">
      <thead><tr>
        <th>Namespace</th><th>Application</th><th>Component</th><th>Cluster</th>
        <th class="sortable" onclick="sortVT('{tid}',4)">Peak&nbsp;Mem</th>
        <th class="sortable" onclick="sortVT('{tid}',5)">vs&nbsp;Base&nbsp;(Mem)</th>
        <th class="sortable" onclick="sortVT('{tid}',6)">Peak&nbsp;CPU</th>
        <th class="sortable" onclick="sortVT('{tid}',7)">vs&nbsp;Base&nbsp;(CPU)</th>
        <th class="sortable" onclick="sortVT('{tid}',8)">#&nbsp;Pods</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</details>
"""


def get_cache_file_path(task_name) -> Any:
    """Generate cache file path based on task name."""
    script_dir = TOOL_DIR
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)

    # Use task name as cache filename (sanitize for filesystem)
    # Replace any characters that might be problematic in filenames
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    return cache_dir / f"{safe_task_name}.json"


def save_recommendations_cache(
    task_name, file_path_or_url, recommendations, margin_pct, base, days, csv_data=None
) -> Any:
    """Save recommendations to cache file based on task name.

    Also saves CSV data and HTML files with timestamp for trend analysis.

    Args:
        task_name: Task name
        file_path_or_url: Original file path or URL
        recommendations: List of recommendation dictionaries
        margin_pct: Margin percentage used
        base: Base metric used
        days: Number of days analyzed
        csv_data: Optional CSV data string to save as HTML

    Returns:
        tuple: (cache_file_path, csv_html_path) - paths to saved files
    """
    cache_file = get_cache_file_path(task_name)
    timestamp = datetime.now()
    timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")

    cache_data = {
        "task_name": task_name,
        "file_path_or_url": file_path_or_url,
        "timestamp": timestamp.isoformat(),
        "margin_pct": margin_pct,
        "base": base,
        "days": days,
        "recommendations": recommendations,
    }

    with open(cache_file, "w") as f:
        json.dump(cache_data, f, indent=2)

    print(
        f"Cached recommendations for task '{task_name}' to: {cache_file}",
        file=sys.stderr,
    )

    # Save CSV as HTML if provided
    csv_html_path = None
    if csv_data:
        csv_html_path = save_csv_to_html(csv_data, task_name, timestamp_str)
        if csv_html_path:
            print(f"Saved CSV data as HTML: {csv_html_path}", file=sys.stderr)

    return cache_file, csv_html_path


def load_recommendations_cache(task_name) -> Any:
    """Load recommendations from cache file based on task name."""
    cache_file = get_cache_file_path(task_name)

    if not cache_file.exists():
        return None

    try:
        with open(cache_file) as f:
            cache_data = json.load(f)

        # Verify it matches the requested task
        cached_task_name = cache_data.get("task_name")
        if cached_task_name == task_name:
            return cache_data
        else:
            print(
                f"Warning: Cache file exists but for different task '{cached_task_name}'."
                f" Ignoring cache.",
                file=sys.stderr,
            )
            return None
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Failed to load cache file: {e}", file=sys.stderr)
        return None


def _save_cluster_partial(task_name, cluster_display, executions, stats) -> Any:
    """Checkpoint one cluster's collected data to disk immediately after it finishes.

    Files land in .analyze_cache/partial/{task}_{cluster}.json so that a restart
    can skip already-completed clusters and load their data from disk instead.
    """
    script_dir = TOOL_DIR
    partial_dir = script_dir / ".analyze_cache" / "partial"
    partial_dir.mkdir(parents=True, exist_ok=True)
    safe_task = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    safe_cluster = re.sub(r"[^a-zA-Z0-9_-]", "_", cluster_display)
    path = partial_dir / f"{safe_task}_{safe_cluster}.json"
    try:
        with open(path, "w") as f:
            json.dump(
                {
                    "task_name": task_name,
                    "cluster": cluster_display,
                    "saved_at": datetime.now().isoformat(),
                    "executions": executions,
                    "stats": {k: v for k, v in stats.items() if k != "debug_samples"},
                },
                f,
            )
        _progress_milestone(f"  [checkpoint] Saved cluster '{cluster_display}' → {path.name}")
    except Exception as e:
        _progress_milestone(
            f"  [checkpoint] Warning: could not save partial for '{cluster_display}': {e}"
        )


def _load_completed_partials(task_name) -> Any:
    """Load all previously checkpointed cluster data for task_name.

    Returns:
        dict  {cluster_display_name: (executions_list, stats_dict)}
              Empty dict if no partials exist or partial dir is missing.
    """
    script_dir = TOOL_DIR
    partial_dir = script_dir / ".analyze_cache" / "partial"
    if not partial_dir.exists():
        return {}
    safe_task = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    result = {}
    for path in sorted(partial_dir.glob(f"{safe_task}_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("task_name") != task_name:
                continue
            cluster = data["cluster"]
            result[cluster] = (data["executions"], data.get("stats", {}))
        except Exception as e:
            _progress_milestone(f"  [checkpoint] Warning: could not load partial {path.name}: {e}")
    return result


def _clear_cluster_partials(task_name) -> Any:
    """Delete all partial checkpoint files for task_name.

    Called when --analyze-again is passed to force a completely fresh collection run.
    """
    script_dir = TOOL_DIR
    partial_dir = script_dir / ".analyze_cache" / "partial"
    if not partial_dir.exists():
        return
    safe_task = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    removed = 0
    for path in partial_dir.glob(f"{safe_task}_*.json"):
        path.unlink()
        removed += 1
    if removed:
        _progress_milestone(
            f"  [checkpoint] Cleared {removed} partial checkpoint(s) for '{task_name}'."
        )


def save_csv_to_html(csv_data, task_name, timestamp_str) -> Any:
    """Save CSV data as HTML table with sortable columns.

    Args:
        csv_data: CSV string with header and data rows
        task_name: Task name for filename
        timestamp_str: Timestamp string for filename (format: YYYYMMDD_HHMMSS)

    Returns:
        Path to saved HTML file
    """
    script_dir = TOOL_DIR
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)

    # Sanitize task name for filename
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    html_filename = f"{safe_task_name}_analyzed_data_{timestamp_str}.html"
    html_path = cache_dir / html_filename

    # Parse CSV using csv module for proper handling of quoted fields
    import io

    lines = [line.strip() for line in csv_data.strip().split("\n") if line.strip()]
    if not lines:
        return None

    # Parse CSV properly
    csv_reader = csv.reader(io.StringIO(csv_data))
    rows = list(csv_reader)

    if not rows:
        return None

    # First row is header
    headers = [h.strip().strip('"') for h in rows[0]]

    # Remaining rows are data
    data_rows = rows[1:] if len(rows) > 1 else []

    # Generate HTML
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resource Usage Data - {task_name}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        h1 {{
            color: #333;
            margin-bottom: 20px;
        }}
        .info {{
            margin-bottom: 20px;
            color: #666;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th {{
            background-color: #4CAF50;
            color: white;
            padding: 12px;
            text-align: left;
            cursor: pointer;
            user-select: none;
            position: relative;
        }}
        th:hover {{
            background-color: #45a049;
        }}
        th::after {{
            content: ' ↕';
            opacity: 0.5;
            margin-left: 5px;
        }}
        th.sorted-asc::after {{
            content: ' ↑';
            opacity: 1;
        }}
        th.sorted-desc::after {{
            content: ' ↓';
            opacity: 1;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }}
        tr:hover {{
            background-color: #f5f5f5;
        }}
        tr:nth-child(even) {{
            background-color: #f9f9f9;
        }}
    </style>
    <script>
        function sortTable(columnIndex) {{
            const table = document.getElementById('dataTable');
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            const header = table.querySelectorAll('th')[columnIndex];
            const isAscending = header.classList.contains('sorted-asc');

            // Remove sort classes from all headers
            table.querySelectorAll('th').forEach(th => {{
                th.classList.remove('sorted-asc', 'sorted-desc');
            }});

            // Sort rows
            rows.sort((a, b) => {{
                const aText = a.cells[columnIndex].textContent.trim();
                const bText = b.cells[columnIndex].textContent.trim();

                // Try numeric comparison first
                const aNum = parseFloat(aText);
                const bNum = parseFloat(bText);
                if (!isNaN(aNum) && !isNaN(bNum)) {{
                    return isAscending ? bNum - aNum : aNum - bNum;
                }}

                // String comparison
                return isAscending ? bText.localeCompare(aText) : aText.localeCompare(bText);
            }});

            // Reorder rows
            rows.forEach(row => tbody.appendChild(row));

            // Add sort class to header
            header.classList.add(isAscending ? 'sorted-desc' : 'sorted-asc');
        }}
    </script>
</head>
<body>
    <h1>Resource Usage Data - {task_name}</h1>
    <div class="info">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
    <table id="dataTable">
        <thead>
            <tr>
"""

    # Add headers with click handlers
    for i, header in enumerate(headers):
        # Strip quotes from header names for cleaner display
        header_cleaned = header.strip().strip('"').strip("'")
        html_content += f'                <th onclick="sortTable({i})">{header_cleaned}</th>\n'

    html_content += """            </tr>
        </thead>
        <tbody>
"""

    # Add data rows
    for row in data_rows:
        html_content += "            <tr>\n"
        for cell in row:
            # Strip quotes from cell value for proper numeric sorting
            # CSV data has quotes around values, but HTML should display without quotes
            cell_cleaned = str(cell).strip().strip('"').strip("'")
            # Escape HTML special characters
            cell_escaped = (
                cell_cleaned.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )
            html_content += f"                <td>{cell_escaped}</td>\n"
        html_content += "            </tr>\n"

    html_content += """        </tbody>
    </table>
</body>
</html>
"""

    # Save file
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return html_path


def save_comparison_table_to_html(
    recommendations, current_resources, task_name, timestamp_str
) -> Any:
    """Save comparison table as HTML (non-sortable).

    Args:
        recommendations: List of recommendation dictionaries
        current_resources: Dictionary of current resources by step name
        task_name: Task name for filename
        timestamp_str: Timestamp string for filename (format: YYYYMMDD_HHMMSS)

    Returns:
        Path to saved HTML file
    """
    script_dir = TOOL_DIR
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)

    # Sanitize task name for filename
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    html_filename = f"{safe_task_name}_comparison_data_{timestamp_str}.html"
    html_path = cache_dir / html_filename

    # Generate HTML
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resource Limits Comparison - {task_name}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        h1 {{
            color: #333;
            margin-bottom: 20px;
        }}
        .info {{
            margin-bottom: 20px;
            color: #666;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th {{
            background-color: #2196F3;
            color: white;
            padding: 12px;
            text-align: left;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }}
        tr:hover {{
            background-color: #f5f5f5;
        }}
        tr:nth-child(even) {{
            background-color: #f9f9f9;
        }}
    </style>
</head>
<body>
    <h1>Resource Limits Comparison: Current vs Proposed</h1>
    <div class="info">Task: {task_name}</div>
    <div class="info">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
    <table>
        <thead>
            <tr>
                <th>Step</th>
                <th>Current Requests</th>
                <th>Proposed Requests</th>
                <th>Current Limits</th>
                <th>Proposed Limits</th>
            </tr>
        </thead>
        <tbody>
"""

    # Add data rows
    for rec in recommendations:
        if rec is None:
            continue

        step_name = rec["step_name"]
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

        # Format values
        curr_req = f"{curr_mem_req} / {curr_cpu_req}"
        curr_lim = f"{curr_mem_lim} / {curr_cpu_lim}"
        prop_req = f"{proposed_mem} / {proposed_cpu}"
        prop_lim = f"{proposed_mem} / {proposed_cpu}"

        html_content += f"""            <tr>
                <td>{step_name_yaml}</td>
                <td>{curr_req}</td>
                <td>{prop_req}</td>
                <td>{curr_lim}</td>
                <td>{prop_lim}</td>
            </tr>
"""

    html_content += """        </tbody>
    </table>
</body>
</html>
"""

    # Save file
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return html_path


def get_date_based_file_path(
    task_name, file_type, date_str, timestamp_str=None, margin_pct=None
) -> Any:
    """Get file path for date-based file naming.

    Args:
        task_name: Task name
        file_type: 'analyzed_data' or 'comparison_data'
        date_str: Date string in YYYYMMDD format
        timestamp_str: Optional timestamp string in YYYYMMDD_HHMMSS format (only used for re-
        analysis)
        margin_pct: Optional margin percentage (only used for comparison_data file type)

    Returns:
        Path object for the file
    """
    script_dir = TOOL_DIR
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)

    if file_type == "comparison_data" and margin_pct is not None:
        # Comparison files include margin in filename
        if timestamp_str:
            filename = f"{safe_task_name}_{file_type}_margin-{margin_pct}_{timestamp_str}"
        else:
            filename = f"{safe_task_name}_{file_type}_margin-{margin_pct}_{date_str}"
    else:
        # Analyzed data files don't include margin
        if timestamp_str:
            filename = f"{safe_task_name}_{file_type}_{timestamp_str}"
        else:
            filename = f"{safe_task_name}_{file_type}_{date_str}"

    return cache_dir / filename


def check_files_exist_for_date(task_name, file_type, date_str, margin_pct=None) -> Any:
    """Check if files already exist for a given date.

    Args:
        task_name: Task name
        file_type: 'analyzed_data' or 'comparison_data'
        date_str: Date string in YYYYMMDD format
        margin_pct: Optional margin percentage (only used for comparison_data)

    Returns:
        True if files exist for this date, False otherwise
    """
    html_path = get_date_based_file_path(
        task_name, file_type, date_str, margin_pct=margin_pct
    ).with_suffix(".html")
    json_path = get_date_based_file_path(
        task_name, file_type, date_str, margin_pct=margin_pct
    ).with_suffix(".json")
    return html_path.exists() or json_path.exists()


def check_comparison_file_exists_for_margin(task_name, date_str, margin_pct) -> Any:
    """Check if comparison file exists for a specific margin.

    Args:
        task_name: Task name
        date_str: Date string in YYYYMMDD format
        margin_pct: Margin percentage

    Returns:
        True if comparison file exists for this margin, False otherwise
    """
    # Check date-only format first
    html_path = get_date_based_file_path(
        task_name, "comparison_data", date_str, margin_pct=margin_pct
    ).with_suffix(".html")
    json_path = get_date_based_file_path(
        task_name, "comparison_data", date_str, margin_pct=margin_pct
    ).with_suffix(".json")

    if html_path.exists() or json_path.exists():
        return True

    # Also check if any timestamped version exists for this margin
    script_dir = TOOL_DIR
    cache_dir = script_dir / ".analyze_cache"

    if not cache_dir.exists():
        return False

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    pattern = f"{safe_task_name}_comparison_data_margin-{margin_pct}_{date_str}_*.json"

    matching_files = list(cache_dir.glob(pattern))
    return len(matching_files) > 0
