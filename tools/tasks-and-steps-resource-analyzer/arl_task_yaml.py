from __future__ import annotations

import html
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from arl_constants import (
    requests,
    yaml,
)
from arl_metrics import get_cluster_display_name, mb_to_kubernetes


def convert_github_url_to_raw(url):
    """Convert GitHub blob URL to raw content URL."""
    # Convert blob URL to raw URL
    # https://github.com/user/repo/blob/branch/path -> https://raw.githubusercontent.com/user/repo/branch/path
    pattern = r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)"
    match = re.match(pattern, url)
    if match:
        user, repo, branch, path = match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
    return url


def fetch_yaml_content(file_path_or_url):
    """Fetch YAML content from file path or URL."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        url = convert_github_url_to_raw(file_path_or_url)
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return yaml.safe_load(response.text), url
        except requests.RequestException as e:
            print(f"Error fetching URL {url}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        path = Path(file_path_or_url)
        if not path.exists():
            print(f"Error: File not found: {file_path_or_url}", file=sys.stderr)
            sys.exit(1)
        with open(path) as f:
            return yaml.safe_load(f), str(path.absolute())


def extract_task_info(yaml_content):
    """Extract task name, step names, and current resource limits from Tekton Task YAML."""
    task_name = yaml_content.get("metadata", {}).get("name", "")
    steps = []
    current_resources = {}

    # Get default resources from stepTemplate
    # Support both Tekton v1 (computeResources) and v1beta1 (resources) field names
    step_template = yaml_content.get("spec", {}).get("stepTemplate", {})
    default_resources = step_template.get("computeResources") or step_template.get("resources", {})
    default_mem_req = default_resources.get("requests", {}).get("memory", "")
    default_cpu_req = default_resources.get("requests", {}).get("cpu", "")
    default_mem_lim = default_resources.get("limits", {}).get("memory", "")
    default_cpu_lim = default_resources.get("limits", {}).get("cpu", "")

    # Extract step names and current resources from spec.steps
    for step in yaml_content.get("spec", {}).get("steps", []):
        step_name = step.get("name", "")
        if step_name:
            steps.append(step_name)

            # Get current resources for this step (use defaults if not specified)
            # Support both Tekton v1 (computeResources) and v1beta1 (resources) field names
            step_resources = step.get("computeResources") or step.get("resources", {})
            step_req = step_resources.get("requests", {})
            step_lim = step_resources.get("limits", {})

            # Get values, using None if not set (to distinguish from empty string)
            mem_req = (
                step_req.get("memory")
                if "memory" in step_req
                else (default_mem_req if default_mem_req else None)
            )
            cpu_req = (
                step_req.get("cpu")
                if "cpu" in step_req
                else (default_cpu_req if default_cpu_req else None)
            )
            mem_lim = (
                step_lim.get("memory")
                if "memory" in step_lim
                else (default_mem_lim if default_mem_lim else None)
            )
            cpu_lim = (
                step_lim.get("cpu")
                if "cpu" in step_lim
                else (default_cpu_lim if default_cpu_lim else None)
            )

            current_resources[step_name] = {
                "requests": {
                    "memory": mem_req,
                    "cpu": cpu_req,
                },
                "limits": {
                    "memory": mem_lim,
                    "cpu": cpu_lim,
                },
            }

    return task_name, steps, default_resources, current_resources


def normalize_step_name_for_compare(name):
    """Strip Tekton step- prefix so YAML names match CSV step column."""
    if not name:
        return ""
    s = str(name).strip()
    if s.startswith("step-"):
        return s[5:]
    return s


def compute_steps_missing_observability(declared_steps, by_step):
    """Declared YAML steps that have no rows in aggregated observability data.

    Args:
        declared_steps: Iterable of step names as in YAML (with or without step- prefix)
        by_step: defaultdict or dict keyed by step name as in CSV (no step- prefix)

    Returns:
        Sorted list of step names (no step- prefix) missing from data.
    """
    declared = {
        normalize_step_name_for_compare(s)
        for s in (declared_steps or [])
        if normalize_step_name_for_compare(s)
    }
    seen = set(by_step.keys()) if by_step else set()
    return sorted(declared - seen)


def _html_steps_missing_observability_banner(steps_missing):
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


def compute_cluster_coverage_report(detailed_executions, days_requested):
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


def _html_cluster_coverage_banner(coverage_report, days_requested):
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


def _html_scrape_interval_note():
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


def compute_heavy_tail_warnings(all_recommendations_by_base):
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


def _html_heavy_tail_warnings_banner(warnings):
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


def _compute_violators_for_step(detailed_executions, step_name, mem_base, cpu_base):
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

    groups = _dd(lambda: {"mem_vals": [], "cpu_vals": []})
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

    result = _dd(lambda: _dd(list))
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
):
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

    def _fmt_mb(mb):
        return f"{mb / 1024:.2f} Gi" if mb >= 1024 else f"{mb:.0f} Mi"

    def _fmt_cpu(c):
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


def read_wrapper_config(wrapper_path):
    """Read TASK_NAME and STEPS from wrapper script.

    Returns:
        tuple: (task_name, steps_list, is_defined)
        - task_name: Task name if defined, None otherwise
        - steps_list: List of step names (without 'step-' prefix) if defined, None otherwise
        - is_defined: True if both TASK_NAME and STEPS are defined (not commented, non-empty)
    """
    task_name = None
    steps_list = None
    is_defined = False

    try:
        with open(wrapper_path) as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue

            # Check for TASK_NAME
            if stripped.startswith("TASK_NAME="):
                # Extract value between quotes
                match = re.search(r'TASK_NAME="([^"]*)"', line)
                if match:
                    task_name = match.group(1).strip()

            # Check for STEPS
            elif stripped.startswith("STEPS="):
                # Extract value between quotes
                match = re.search(r'STEPS="([^"]*)"', line)
                if match:
                    steps_str = match.group(1).strip()
                    if steps_str:
                        # Split by space and remove 'step-' prefix
                        steps_list = [normalize_step_name_for_compare(s) for s in steps_str.split()]

        # Both must be defined and non-empty
        is_defined = (
            task_name is not None
            and task_name != ""
            and steps_list is not None
            and len(steps_list) > 0
        )

    except Exception as e:
        print(f"Warning: Could not read wrapper script: {e}", file=sys.stderr)

    return task_name, steps_list, is_defined


def validate_wrapper_steps(wrapper_task, wrapper_steps, yaml_task, yaml_steps):
    """Validate wrapper-defined task and steps against YAML file.

    Args:
        wrapper_task: Task name from wrapper script
        wrapper_steps: List of step names from wrapper (without 'step-' prefix)
        yaml_task: Task name from YAML file
        yaml_steps: List of step names from YAML file

    Returns:
        tuple: (is_valid, error_messages)
        - is_valid: True if validation passes
        - error_messages: List of error messages (empty if valid)
    """
    errors = []

    # Check task name match (case-sensitive)
    if wrapper_task != yaml_task:
        errors.append(
            f"Task name mismatch: wrapper has '{wrapper_task}', YAML file has '{yaml_task}'"
        )

    # Convert to sets for comparison (normalize step names)
    wrapper_steps_set = set(wrapper_steps)
    yaml_steps_set = set(yaml_steps)

    # Check if wrapper steps are subset or equal to YAML steps
    extra_steps = wrapper_steps_set - yaml_steps_set
    if extra_steps:
        errors.append(f"Wrapper defines steps not found in YAML file: {sorted(extra_steps)}")

    missing_steps = yaml_steps_set - wrapper_steps_set
    if missing_steps:
        # This is a warning, not an error (wrapper can be a subset)
        pass

    is_valid = len(errors) == 0
    return is_valid, errors


def check_cluster_connectivity(wrapper_path):
    """Check connectivity to all clusters defined in wrapper script.

    Returns:
        tuple: (all_connected, connectivity_report)
        - all_connected: True if all clusters are accessible
        - connectivity_report: List of (cluster_display_name, status, error_message) tuples
                              Note: cluster_display_name is the short name for display purposes
    """
    report = []
    all_connected = True

    try:
        with open(wrapper_path) as f:
            lines = f.readlines()

        # Extract CONTEXTS line (only non-commented lines, similar to read_wrapper_config)
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
            # No CONTEXTS found in non-commented lines, try to get contexts from kubectl as fallback
            result = subprocess.run(
                ["kubectl", "config", "get-contexts", "-o", "name"],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minutes timeout for connectivity check
            )
            if result.returncode == 0:
                contexts = [c.strip() for c in result.stdout.strip().split("\n") if c.strip()]
            else:
                return False, [("unknown", False, "Could not get cluster contexts")]
        else:
            # Handle cases where CONTEXTS uses command substitution
            if "$(" in contexts_str:
                # Execute command substitution to get contexts
                # Extract the command inside $()
                cmd_match = re.search(r"\$\(([^)]+)\)", contexts_str)
                if cmd_match:
                    cmd = cmd_match.group(1).strip()
                    # Handle the specific case: kubectl config get-contexts -o name 2>/dev/null |
                    # xargs
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
                            # Fallback to default if command fails (extract from echo)
                            fallback_match = re.search(r'echo\s+[\'"]([^\'"]+)[\'"]', contexts_str)
                            if fallback_match:
                                contexts = [fallback_match.group(1).strip()]
                            else:
                                return False, [
                                    (
                                        "unknown",
                                        False,
                                        "Could not execute CONTEXTS command",
                                    )
                                ]
                    else:
                        return False, [("unknown", False, f"Unsupported CONTEXTS command: {cmd}")]
                else:
                    return False, [("unknown", False, "Invalid CONTEXTS command substitution")]
            else:
                # Simple string value - split by space
                contexts = [c.strip() for c in contexts_str.split() if c.strip()]

        # Test connectivity to each cluster
        for ctx in contexts:
            if not ctx:
                continue
            try:
                result = subprocess.run(
                    ["kubectl", "config", "use-context", ctx],
                    capture_output=True,
                    text=True,
                    timeout=120,  # 2 minutes timeout for connectivity check
                )
                if result.returncode == 0:
                    # Try a simple kubectl command to verify connectivity
                    test_result = subprocess.run(
                        ["kubectl", "get", "namespaces", "--request-timeout=2m"],
                        capture_output=True,
                        text=True,
                        timeout=120,  # 2 minutes timeout for connectivity check
                    )
                    if test_result.returncode == 0:
                        # Use display name for report, but ctx (full context) is still used for
                        # operations
                        display_name = get_cluster_display_name(ctx)
                        report.append((display_name, True, "Connected"))
                    else:
                        display_name = get_cluster_display_name(ctx)
                        report.append(
                            (
                                display_name,
                                False,
                                f"Cannot access cluster: {test_result.stderr[:100]}",
                            )
                        )
                        all_connected = False
                else:
                    display_name = get_cluster_display_name(ctx)
                    report.append(
                        (
                            display_name,
                            False,
                            f"Cannot switch context: {result.stderr[:100]}",
                        )
                    )
                    all_connected = False
            except subprocess.TimeoutExpired:
                display_name = get_cluster_display_name(ctx)
                report.append((display_name, False, "Connection timeout"))
                all_connected = False
            except Exception as e:
                display_name = get_cluster_display_name(ctx)
                report.append((display_name, False, f"Error: {str(e)[:100]}"))
                all_connected = False

    except Exception as e:
        return False, [("unknown", False, f"Error reading wrapper script: {str(e)}")]

    return all_connected, report
