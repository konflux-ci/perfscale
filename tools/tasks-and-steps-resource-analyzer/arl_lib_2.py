from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from arl_lib_0 import normalize_step_name_for_compare
from arl_lib_1 import _progress_milestone, cores_to_kubernetes, mb_to_kubernetes, parse_cpu_value
from arl_lib_3 import save_comparison_table_to_html


def analyze_step_data_all_bases(step_name, step_rows, margin_pct=5):
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


def print_comparison_table(recommendations, current_resources=None, task_name=None, save_html=True):
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
):
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


def get_cache_file_path(task_name):
    """Generate cache file path based on task name."""
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)

    # Use task name as cache filename (sanitize for filesystem)
    # Replace any characters that might be problematic in filenames
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    return cache_dir / f"{safe_task_name}.json"


def save_recommendations_cache(
    task_name, file_path_or_url, recommendations, margin_pct, base, days, csv_data=None
):
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


def load_recommendations_cache(task_name):
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


def _save_cluster_partial(task_name, cluster_display, executions, stats):
    """Checkpoint one cluster's collected data to disk immediately after it finishes.

    Files land in .analyze_cache/partial/{task}_{cluster}.json so that a restart
    can skip already-completed clusters and load their data from disk instead.
    """
    script_dir = Path(__file__).parent
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


def _load_completed_partials(task_name):
    """Load all previously checkpointed cluster data for task_name.

    Returns:
        dict  {cluster_display_name: (executions_list, stats_dict)}
              Empty dict if no partials exist or partial dir is missing.
    """
    script_dir = Path(__file__).parent
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


def _clear_cluster_partials(task_name):
    """Delete all partial checkpoint files for task_name.

    Called when --analyze-again is passed to force a completely fresh collection run.
    """
    script_dir = Path(__file__).parent
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


def save_csv_to_html(csv_data, task_name, timestamp_str):
    """Save CSV data as HTML table with sortable columns.

    Args:
        csv_data: CSV string with header and data rows
        task_name: Task name for filename
        timestamp_str: Timestamp string for filename (format: YYYYMMDD_HHMMSS)

    Returns:
        Path to saved HTML file
    """
    script_dir = Path(__file__).parent
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
