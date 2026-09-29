from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from arl_html_output import get_date_based_file_path
from arl_task_yaml import (
    _compute_violators_for_step,
    _html_cluster_coverage_banner,
    _html_heavy_tail_warnings_banner,
    _html_scrape_interval_note,
    _html_steps_missing_observability_banner,
    _html_violators_block,
    compute_heavy_tail_warnings,
    normalize_step_name_for_compare,
)


def save_comparison_data_all_bases(
    task_name,
    all_recommendations_by_base,
    current_resources,
    margin_pct,
    date_str,
    use_timestamp=False,
    steps_without_observability_data=None,
    cluster_coverage_report=None,
    days_requested=None,
    detailed_executions=None,
):
    """Save comparison data for all base metrics as HTML and JSON.

    Args:
        task_name: Task name
        all_recommendations_by_base: Dict with keys 'max', 'p95', 'p90', 'median', each containing
        list of recommendations
        current_resources: Dictionary of current resources by step name
        margin_pct: Margin percentage used
        date_str: Date string in YYYYMMDD format
        use_timestamp: If True, add timestamp to filename (used when re-analysis happens in Phase 1)
        steps_without_observability_data: Optional list of YAML steps with no Prometheus rows (Phase
        1 only)
        cluster_coverage_report: Optional dict from compute_cluster_coverage_report()
        days_requested: Number of days requested (for coverage banner)
        detailed_executions: Optional list of per-pod execution dicts (enables violators sub-tables)

    Returns:
        tuple: (html_path, json_path) - paths to saved files
    """
    # If re-analysis happened (use_timestamp=True), use timestamp
    # Otherwise, just use date (for Phase 1 first run or Phase 2 with different margin)
    if use_timestamp:
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = get_date_based_file_path(
            task_name, "comparison_data", date_str, timestamp_str, margin_pct
        ).with_suffix(".html")
        json_path = get_date_based_file_path(
            task_name, "comparison_data", date_str, timestamp_str, margin_pct
        ).with_suffix(".json")
    else:
        html_path = get_date_based_file_path(
            task_name, "comparison_data", date_str, margin_pct=margin_pct
        ).with_suffix(".html")
        json_path = get_date_based_file_path(
            task_name, "comparison_data", date_str, margin_pct=margin_pct
        ).with_suffix(".json")

    banner_cmp = _html_steps_missing_observability_banner(steps_without_observability_data or [])
    coverage_banner = _html_cluster_coverage_banner(
        cluster_coverage_report or {}, days_requested or 0
    )
    tail_warnings = compute_heavy_tail_warnings(all_recommendations_by_base)
    tail_banner = _html_heavy_tail_warnings_banner(tail_warnings)
    scrape_note = _html_scrape_interval_note()
    # Generate HTML with separate tables for each base metric
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
            margin-bottom: 10px;
        }}
        h2 {{
            color: #555;
            margin-top: 30px;
            margin-bottom: 15px;
            border-bottom: 2px solid #2196F3;
            padding-bottom: 5px;
        }}
        .info {{
            margin-bottom: 20px;
            color: #666;
        }}
        /* main comparison table */
        table.main-cmp {{
            border-collapse: collapse;
            width: 100%;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 4px;
        }}
        table.main-cmp th {{
            background-color: #2196F3;
            color: white;
            padding: 11px 12px;
            text-align: left;
        }}
        table.main-cmp td {{
            padding: 10px 12px;
            border-bottom: 1px solid #ddd;
        }}
        table.main-cmp tr:hover {{ background-color: #f0f7ff; }}
        table.main-cmp tr:nth-child(even) {{ background-color: #f9f9f9; }}

        /* violators collapsible block */
        details.violators-block {{
            background: #fffbf0;
            border: 1px solid #ffe082;
            border-radius: 4px;
            margin-bottom: 20px;
        }}
        summary.violators-summary {{
            cursor: pointer;
            padding: 8px 14px;
            font-size: 0.92em;
            color: #7b4f00;
            list-style: none;
            user-select: none;
        }}
        summary.violators-summary::-webkit-details-marker {{ display: none; }}
        summary.violators-summary:hover {{ background: #fff3cd; border-radius: 4px; }}
        .violators-body {{ padding: 0 14px 14px; }}
        .no-violators {{
            color: #388e3c;
            font-size: 0.88em;
            padding: 4px 0 16px 2px;
            font-style: italic;
            margin: 0 0 16px 0;
        }}

        /* violators inner table */
        table.violators-table {{
            border-collapse: collapse;
            width: 100%;
            font-size: 0.88em;
            background: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}
        table.violators-table th {{
            background: #fff3cd;
            color: #5d4037;
            padding: 8px 10px;
            text-align: left;
            border-bottom: 2px solid #ffe082;
        }}
        table.violators-table td {{
            padding: 7px 10px;
            border-bottom: 1px solid #f0f0f0;
            vertical-align: middle;
        }}
        td.viol-ns-cell {{
            font-weight: bold;
            color: #1565c0;
            border-right: 2px solid #bbdefb;
            background: #e3f2fd;
        }}
        td.viol-app-cell {{
            color: #2e7d32;
            border-right: 1px solid #c8e6c9;
            background: #f1f8f1;
        }}
        td.viol-cell {{ color: #c62828; font-weight: bold; }}
        .cluster-badge {{
            background: #e8eaf6;
            color: #303f9f;
            font-size: 0.82em;
            padding: 2px 6px;
            border-radius: 3px;
            white-space: nowrap;
        }}
        /* sortable violators column headers */
        table.violators-table th.sortable {{
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
            background: #ffe082;
        }}
        table.violators-table th.sortable:hover {{ background: #ffd54f; }}
        table.violators-table th.sort-asc::after  {{ content: " ↑"; font-weight: bold; }}
        table.violators-table th.sort-desc::after {{ content: " ↓"; font-weight: bold; }}
        .sort-hint {{ font-size: 0.78em; color: #888; margin: 4px 0 6px 0; }}
    </style>
    <script>
    function sortVT(tableId, colIdx) {{
        var tbl   = document.getElementById(tableId);
        var ths   = tbl.querySelectorAll('thead th');
        var tbody = tbl.querySelector('tbody');
        var rows  = Array.from(tbody.querySelectorAll('tr'));
        var th    = ths[colIdx];
        var asc   = th.classList.contains('sort-desc');
        ths.forEach(function(h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});
        rows.sort(function(a, b) {{
            var av = parseFloat(a.cells[colIdx].getAttribute('data-val'));
            var bv = parseFloat(b.cells[colIdx].getAttribute('data-val'));
            if (!isNaN(av) && !isNaN(bv)) return asc ? av - bv : bv - av;
            var at = a.cells[colIdx].textContent.trim();
            var bt = b.cells[colIdx].textContent.trim();
            return asc ? at.localeCompare(bt) : bt.localeCompare(at);
        }});
        rows.forEach(function(r) {{ tbody.appendChild(r); }});
        th.classList.add(asc ? 'sort-asc' : 'sort-desc');
    }}
    </script>
</head>
<body>
    <h1>Resource Limits Comparison: Current vs Proposed</h1>
    <div class="info">Task: {task_name}</div>
    <div class="info">Margin: {margin_pct}%</div>
    <div class="info">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
{coverage_banner}{tail_banner}{scrape_note}{banner_cmp}
"""

    # Generate table + violators for each base metric
    for base in ["max", "p95", "p90", "median"]:
        recommendations = all_recommendations_by_base.get(base, [])
        if not recommendations:
            continue

        base_label = base.upper() if base != "max" else "MAX"
        html_content += f"""
    <h2>Base Metric: {base_label} (Margin: {margin_pct}%)</h2>
    <table class="main-cmp">
        <thead>
            <tr>
                <th>Step</th>
                <th>Current Requests<br><small>(mem / cpu)</small></th>
                <th>Proposed Requests<br><small>(mem / cpu)</small></th>
                <th>Current Limits<br><small>(mem / cpu)</small></th>
                <th>Proposed Limits<br><small>(mem / cpu)</small></th>
            </tr>
        </thead>
        <tbody>
"""

        for rec in recommendations:
            if rec is None:
                continue

            step_name = rec["step_name"]
            step_name_yaml = normalize_step_name_for_compare(step_name)
            proposed_mem = rec["mem_recommended_k8s"]
            proposed_cpu = rec["cpu_recommended_k8s"]

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

            curr_req = f"{curr_mem_req} / {curr_cpu_req}"
            curr_lim = f"{curr_mem_lim} / {curr_cpu_lim}"
            prop_req = f"{proposed_mem} / {proposed_cpu}"
            prop_lim = f"{proposed_mem} / {proposed_cpu}"

            html_content += f"""            <tr>
                <td><strong>{step_name_yaml}</strong></td>
                <td>{curr_req}</td>
                <td>{prop_req}</td>
                <td>{curr_lim}</td>
                <td>{prop_lim}</td>
            </tr>
"""

        html_content += "        </tbody>\n    </table>\n"

        # Violators block (one per step, only when detailed_executions are available)
        if detailed_executions:
            for rec in recommendations:
                if rec is None:
                    continue
                step_name = rec["step_name"]
                step_disp = normalize_step_name_for_compare(step_name)
                mem_base = rec.get("mem_base", 0)
                cpu_base = rec.get("cpu_base", 0)
                viol = _compute_violators_for_step(
                    detailed_executions, step_name, mem_base, cpu_base
                )
                html_content += _html_violators_block(
                    viol, mem_base, cpu_base, base_label, step_disp
                )

    html_content += """</body>
</html>
"""

    # Save HTML
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # Save JSON
    json_data = {
        "task_name": task_name,
        "date": date_str,
        "timestamp": datetime.now().isoformat(),
        "margin_pct": margin_pct,
        "recommendations_by_base": all_recommendations_by_base,
        "steps_without_observability_data": list(steps_without_observability_data or []),
        "cluster_coverage_report": cluster_coverage_report or {},
        "days_requested": days_requested or 0,
        "heavy_tail_warnings": tail_warnings,
        "current_resources": current_resources or {},
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    return html_path, json_path


def load_analyzed_data(task_name, date_str):
    """Load analyzed data from JSON file.

    Tries date-only format first, then looks for latest date+timestamp format.

    Args:
        task_name: Task name
        date_str: Date string in YYYYMMDD format

    Returns:
        Dictionary with analyzed data or None if not found
    """
    # Try date-only format first
    json_path = get_date_based_file_path(task_name, "analyzed_data", date_str).with_suffix(".json")

    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Failed to load analyzed data: {e}", file=sys.stderr)
            return None

    # If not found, look for latest date+timestamp format
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"

    if not cache_dir.exists():
        return None

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    pattern = f"{safe_task_name}_analyzed_data_{date_str}_*.json"

    matching_files = list(cache_dir.glob(pattern))
    if not matching_files:
        return None

    # Sort by modification time (newest first) and load the latest
    matching_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_file = matching_files[0]

    try:
        with open(latest_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Failed to load analyzed data: {e}", file=sys.stderr)
        return None


def load_comparison_data(task_name, date_str, margin_pct):
    """Load comparison data from JSON file for a specific margin.

    Tries date-only format first, then looks for latest date+timestamp format.

    Args:
        task_name: Task name
        date_str: Date string in YYYYMMDD format
        margin_pct: Margin percentage

    Returns:
        Dictionary with comparison data or None if not found
    """
    # Try date-only format first
    json_path = get_date_based_file_path(
        task_name, "comparison_data", date_str, margin_pct=margin_pct
    ).with_suffix(".json")

    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Failed to load comparison data: {e}", file=sys.stderr)
            return None

    # If not found, look for latest date+timestamp format with this margin
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"

    if not cache_dir.exists():
        return None

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    pattern = f"{safe_task_name}_comparison_data_margin-{margin_pct}_{date_str}_*.json"

    matching_files = list(cache_dir.glob(pattern))
    if not matching_files:
        return None

    # Sort by modification time (newest first) and load the latest
    matching_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_file = matching_files[0]

    try:
        with open(latest_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Failed to load comparison data: {e}", file=sys.stderr)
        return None


def find_latest_analysis_date(task_name):
    """Find the latest analysis date for a task.

    Handles both date-only (YYYYMMDD) and date+timestamp (YYYYMMDD_HHMMSS) formats.

    Args:
        task_name: Task name

    Returns:
        Date string in YYYYMMDD format or None if not found
    """
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"

    if not cache_dir.exists():
        return None

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    pattern = f"{safe_task_name}_analyzed_data_*.json"

    matching_files = list(cache_dir.glob(pattern))
    if not matching_files:
        return None

    # Extract dates and find the latest
    dates = set()
    for file_path in matching_files:
        # Extract date from filename: task_analyzed_data_YYYYMMDD.json or
        # task_analyzed_data_YYYYMMDD_HHMMSS.json
        match = re.search(r"_analyzed_data_(\d{8})(?:_\d{6})?\.json$", file_path.name)
        if match:
            dates.add(match.group(1))  # Extract just the date part (YYYYMMDD)

    if not dates:
        return None

    # Return the latest date (already in YYYYMMDD format, so lexicographic sort works)
    return max(dates)


def _query_prometheus_range(session, host, token, query, start, end, timeout=900):
    """Query Prometheus /api/v1/query_range in-process; returns response JSON dict."""
    url = f"https://{host}/api/v1/query_range"
    duration = int(end) - int(start)
    if duration <= 86400:
        step = "30s"
    elif duration <= 604800:
        step = "5m"
    elif duration <= 2592000:
        step = "15m"
    else:
        step = "1h"
    resp = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"query": query, "start": start, "end": end, "step": step},
        verify=False,  # nosec B501
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _list_task_pods(session, host, token, task_name, end_time_secs, lookback_seconds):
    """List pods for a task via Prometheus kube_pod_labels; returns response JSON dict."""
    if lookback_seconds <= 0:
        lookback_seconds = 86400
    step = max(15, lookback_seconds // 5760)
    resp = session.get(
        f"https://{host}/api/v1/query_range",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "query": (
                f'kube_pod_labels{{label_tekton_dev_task="{task_name}",namespace=~".*-tenant"}}'
            ),
            "step": step,
            "start": end_time_secs - lookback_seconds,
            "end": end_time_secs,
        },
        verify=False,  # nosec B501
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _get_component_for_pod(session, host, token, pod, namespace, end_time, days):
    """Get component/application labels from Prometheus kube_pod_labels.

    Returns (component, application) strings; each defaults to "N/A".
    """
    _COMPONENT_KEYS = [
        "label_appstudio_openshift_io_component",
        "label_appstudio_redhat_com_component",
        "appstudio_openshift_io_component",
        "appstudio_redhat_com_component",
        "label_appstudio.redhat.com/component",
        "label_appstudio.openshift.io/component",
        "appstudio.redhat.com/component",
        "appstudio.openshift.io/component",
        "label_component",
        "component",
        "label_app_kubernetes_io_component",
        "app.kubernetes.io/component",
        "app_kubernetes_io_component",
    ]
    _APPLICATION_KEYS = [
        "label_appstudio_openshift_io_application",
        "label_appstudio_redhat_com_application",
        "appstudio_openshift_io_application",
        "appstudio_redhat_com_application",
        "label_appstudio.redhat.com/application",
        "label_appstudio.openshift.io/application",
        "appstudio.redhat.com/application",
        "appstudio.openshift.io/application",
        "label_application",
        "application",
        "label_app_kubernetes_io_name",
        "app.kubernetes.io/name",
        "app_kubernetes_io_name",
        "label_app",
        "app",
    ]

    def _first_present(mapping, keys):
        for key in keys:
            value = mapping.get(key)
            if value:
                return value
        return "N/A"

    def _fetch(query, use_range):
        if use_range:
            try:
                start_ts = int(end_time) - (days * 24 * 60 * 60)
                url = f"https://{host}/api/v1/query_range"
                params = {
                    "query": query,
                    "start": start_ts,
                    "end": int(end_time),
                    "step": f"{days * 15}s",
                }
            except (ValueError, TypeError):
                url = f"https://{host}/api/v1/query"
                params = {"query": query}
        else:
            url = f"https://{host}/api/v1/query"
            params = {"query": query}
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            verify=False,  # nosec B501
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("data", {}).get("result", [])

    use_range = bool(end_time and end_time != "N/A" and end_time != "" and days)
    ns_valid = bool(namespace and namespace != "N/A" and namespace != "")

    q_with_ns = f'kube_pod_labels{{pod="{pod}",namespace="{namespace}"}}'
    q_no_ns = f'kube_pod_labels{{pod="{pod}"}}'

    try:
        data = _fetch(q_with_ns if ns_valid else q_no_ns, use_range)
        if not data and ns_valid:
            data = _fetch(q_no_ns, use_range)
    except Exception:  # nosec B110
        data = []

    if not data:
        return "N/A", "N/A"

    metric = data[0].get("metric", {}) if isinstance(data[0], dict) else {}
    return _first_present(metric, _COMPONENT_KEYS), _first_present(metric, _APPLICATION_KEYS)


def extract_component_from_pod(pod_name, namespace, token, prom_host, end_time, days, session=None):
    """Extract component and application from pod labels or namespace/pod name.

    Args:
        pod_name: Pod name
        namespace: Namespace
        token: Prometheus token
        prom_host: Prometheus host
        end_time: End time for query
        days: Number of days

    Returns:
        Tuple (component, application); each is a string or "N/A"
    """
    try:
        # Use in-process HTTP when a session is provided, else fall back to subprocess.
        if session is not None:
            try:
                component, application = _get_component_for_pod(
                    session,
                    prom_host,
                    token,
                    pod_name,
                    namespace or "N/A",
                    end_time,
                    days,
                )
                if component and component != "N/A":
                    return (component, application if application else "N/A")
            except Exception:  # nosec B110
                pass
        else:
            script_dir = Path(__file__).parent
            component_script = script_dir / "get_component_for_pod.py"

            if component_script.exists():
                result = subprocess.run(
                    [
                        sys.executable,
                        str(component_script),
                        token,
                        prom_host,
                        pod_name,
                        namespace or "N/A",
                        str(end_time),
                        str(days),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode == 0:
                    try:
                        component_data = json.loads(result.stdout)
                        component = component_data.get("component", "N/A")
                        application = component_data.get("application", "N/A")
                        if component and component != "N/A":
                            return (component, application if application else "N/A")
                    except (json.JSONDecodeError, KeyError):
                        pass

        # Fallback: Try to extract from namespace/pod name
        component_fallback = "N/A"
        if namespace and namespace != "N/A" and namespace.endswith("-tenant"):
            potential_component = namespace[:-7]  # Remove "-tenant"
            if potential_component:
                component_fallback = potential_component
        if component_fallback == "N/A" and pod_name:
            parts = pod_name.split("-")
            if len(parts) >= 2:
                potential_component = parts[0]
                if potential_component and len(potential_component) > 1:
                    component_fallback = potential_component

        return (component_fallback, "N/A")
    except Exception as e:
        if globals().get("args") and globals()["args"].debug:
            print(
                f"DEBUG: Error extracting component for pod {pod_name}: {e}",
                file=sys.stderr,
            )
        return ("N/A", "N/A")
