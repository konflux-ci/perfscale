from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from arl_task_yaml import (
    _html_cluster_coverage_banner,
    _html_scrape_interval_note,
    _html_steps_missing_observability_banner,
    normalize_step_name_for_compare,
)


def save_comparison_table_to_html(recommendations, current_resources, task_name, timestamp_str):
    """Save comparison table as HTML (non-sortable).

    Args:
        recommendations: List of recommendation dictionaries
        current_resources: Dictionary of current resources by step name
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


def get_date_based_file_path(task_name, file_type, date_str, timestamp_str=None, margin_pct=None):
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
    script_dir = Path(__file__).parent
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


def check_files_exist_for_date(task_name, file_type, date_str, margin_pct=None):
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


def check_comparison_file_exists_for_margin(task_name, date_str, margin_pct):
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
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"

    if not cache_dir.exists():
        return False

    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    pattern = f"{safe_task_name}_comparison_data_margin-{margin_pct}_{date_str}_*.json"

    matching_files = list(cache_dir.glob(pattern))
    return len(matching_files) > 0


def save_analyzed_data(
    task_name,
    csv_data,
    date_str,
    steps_without_observability_data=None,
    cluster_coverage_report=None,
    days_requested=None,
):
    """Save analyzed data (CSV) as HTML and JSON files.

    Args:
        task_name: Task name
        csv_data: CSV data string
        date_str: Date string in YYYYMMDD format
        steps_without_observability_data: Optional list of YAML step names with no Prometheus rows
        cluster_coverage_report: Optional dict from compute_cluster_coverage_report()
        days_requested: Number of days requested (for coverage banner)

    Returns:
        tuple: (html_path, json_path) - paths to saved files
    """
    # If files exist for this date, use timestamp to preserve old files
    if check_files_exist_for_date(task_name, "analyzed_data", date_str):
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = get_date_based_file_path(
            task_name, "analyzed_data", date_str, timestamp_str
        ).with_suffix(".html")
        json_path = get_date_based_file_path(
            task_name, "analyzed_data", date_str, timestamp_str
        ).with_suffix(".json")
    else:
        html_path = get_date_based_file_path(task_name, "analyzed_data", date_str).with_suffix(
            ".html"
        )
        json_path = get_date_based_file_path(task_name, "analyzed_data", date_str).with_suffix(
            ".json"
        )

    # Save HTML (reuse existing function but with date-based naming)
    if csv_data:
        import io

        csv_reader = csv.reader(io.StringIO(csv_data))
        rows = list(csv_reader)

        if rows:
            headers = [h.strip().strip('"') for h in rows[0]]
            data_rows = rows[1:] if len(rows) > 1 else []
            banner_html = _html_steps_missing_observability_banner(steps_without_observability_data)
            coverage_banner = _html_cluster_coverage_banner(
                cluster_coverage_report or {}, days_requested or 0
            )
            scrape_note = _html_scrape_interval_note()

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

            table.querySelectorAll('th').forEach(th => {{
                th.classList.remove('sorted-asc', 'sorted-desc');
            }});

            rows.sort((a, b) => {{
                const aText = a.cells[columnIndex].textContent.trim();
                const bText = b.cells[columnIndex].textContent.trim();

                const aNum = parseFloat(aText);
                const bNum = parseFloat(bText);
                if (!isNaN(aNum) && !isNaN(bNum)) {{
                    return isAscending ? bNum - aNum : aNum - bNum;
                }}

                return isAscending ? bText.localeCompare(aText) : aText.localeCompare(bText);
            }});

            rows.forEach(row => tbody.appendChild(row));
            header.classList.add(isAscending ? 'sorted-desc' : 'sorted-asc');
        }}
    </script>
</head>
<body>
    <h1>Resource Usage Data - {task_name}</h1>
    <div class="info">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
{coverage_banner}{scrape_note}{banner_html}    <table id="dataTable">
        <thead>
            <tr>
"""

            for i, header in enumerate(headers):
                header_cleaned = header.strip().strip('"').strip("'")
                html_content += (
                    f'                <th onclick="sortTable({i})">{header_cleaned}</th>\n'  # noqa: E501
                )

            html_content += """            </tr>
        </thead>
        <tbody>
"""

            for row in data_rows:
                html_content += "            <tr>\n"
                for cell in row:
                    cell_cleaned = str(cell).strip().strip('"').strip("'")
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

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

    # Save JSON (convert CSV to JSON structure)
    json_data = {
        "task_name": task_name,
        "date": date_str,
        "timestamp": datetime.now().isoformat(),
        "csv_data": csv_data,
        "steps_without_observability_data": list(steps_without_observability_data or []),
        "cluster_coverage_report": cluster_coverage_report or {},
        "days_requested": days_requested or 0,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    return html_path, json_path


def _write_one_step_detailed_files(
    cache_dir, task_name, step_name, step_executions, date_str, timestamp_suffix=None
):
    """Write HTML, JSON, and CSV for a single step.

    Used by save_detailed_per_step_data.
    Column order: Cluster, Namespace, Task, Step, Component, Application, Pod,
    Final Memory Usage (MB), Current Mem requests, Current Mem Limits,
    Final CPU Usage (Cores), Current CPU requests, Current CPU Limits, Timestamp.
    Step name is displayed without 'step-' prefix. Current * values are Kubernetes format (e.g.
    512Mi, 1000m).

    Returns:
        tuple: (html_path, json_path, csv_path)
    """
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    safe_step_name = re.sub(r"[^a-zA-Z0-9_-]", "_", step_name)
    if timestamp_suffix:
        base = (
            f"{safe_task_name}_analyzed_data_detailed_step"
            f"_{safe_step_name}_{date_str}"
            f"_{timestamp_suffix}"
        )
    else:
        base = f"{safe_task_name}_analyzed_data_detailed_step_{safe_step_name}_{date_str}"
    html_path = cache_dir / f"{base}.html"
    json_path = cache_dir / f"{base}.json"
    csv_path = cache_dir / f"{base}.csv"

    table_id = f"table_{safe_step_name}"
    # Numeric columns for sort: 7 = Final Memory, 10 = Final CPU, 11 = I/O Read, 12 = I/O Write
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Detailed Resource Usage - {task_name} / {step_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
        h1 {{ color: #333; margin-bottom: 10px; }}
        .info {{ margin-bottom: 20px; color: #666; }}
        .table-wrapper {{ overflow-x: auto; width: 100%; }}
        table {{ border-collapse: collapse; min-width: 100%;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        th {{ background-color: #4CAF50; color: white;
            padding: 12px; text-align: left;
            cursor: pointer; user-select: none;
            position: relative; white-space: normal;
            word-wrap: break-word; max-width: 120px; }}
        th:hover {{ background-color: #45a049; }}
        th::after {{ content: ' ↕'; opacity: 0.5; margin-left: 5px; }}
        th.sorted-asc::after {{ content: ' ↑'; opacity: 1; }}
        th.sorted-desc::after {{ content: ' ↓'; opacity: 1; }}
        td {{ padding: 10px; border-bottom: 1px solid #ddd; }}
        tr:hover {{ background-color: #f5f5f5; }}
        tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .io-high {{ background-color: #fff3cd !important; font-weight: bold; }}
    </style>
    <script>
        function sortTable(tableId, columnIndex) {{
            const table = document.getElementById(tableId);
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            const header = table.querySelectorAll('th')[columnIndex];
            const isAscending = header.classList.contains('sorted-asc');
            const isNumeric = (columnIndex === 7
                || columnIndex === 10
                || columnIndex === 11
                || columnIndex === 12);
            table.querySelectorAll('th').forEach(
                th => th.classList.remove(
                    'sorted-asc', 'sorted-desc'));
            rows.sort((a, b) => {{
                const aText = a.cells[columnIndex].textContent.trim();
                const bText = b.cells[columnIndex].textContent.trim();
                if (isNumeric) {{
                    const aNum = parseFloat(aText);
                    const bNum = parseFloat(bText);
                    if (!isNaN(aNum) && !isNaN(bNum))
                        return isAscending
                            ? bNum - aNum : aNum - bNum;
                }}
                return isAscending ? bText.localeCompare(aText) : aText.localeCompare(bText);
            }});
            rows.forEach(row => tbody.appendChild(row));
            header.classList.add(isAscending ? 'sorted-desc' : 'sorted-asc');
        }}
    </script>
</head>
<body>
    <h1>Historical Resource Utilization: {task_name} &ndash; {step_name}</h1>
    <div class="info">Task: {task_name}</div>
    <div class="info">Step: {step_name}</div>
    <div class="info">Pod Executions: {len(step_executions)}</div>
    <div class="info">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
    <div class="table-wrapper">
    <table id="{table_id}">
        <thead>
            <tr>
                <th onclick="sortTable('{table_id}', 0)">Cluster</th>
                <th onclick="sortTable('{table_id}', 1)">Namespace</th>
                <th onclick="sortTable('{table_id}', 2)">Task</th>
                <th onclick="sortTable('{table_id}', 3)">Step</th>
                <th onclick="sortTable('{table_id}', 4)">Component</th>
                <th onclick="sortTable('{table_id}', 5)">Application</th>
                <th onclick="sortTable('{table_id}', 6)">Pod</th>
                <th onclick="sortTable('{table_id}', 7)">Final Memory Usage (MB)</th>
                <th onclick="sortTable('{table_id}', 8)">Current Mem requests</th>
                <th onclick="sortTable('{table_id}', 9)">Current Mem Limits</th>
                <th onclick="sortTable('{table_id}', 10)">Final CPU Usage (Cores)</th>
                <th onclick="sortTable('{table_id}', 11)">Peak Disk Read (MB/s)</th>
                <th onclick="sortTable('{table_id}', 12)">Peak Disk Write (MB/s)</th>
                <th onclick="sortTable('{table_id}', 13)">Current CPU requests</th>
                <th onclick="sortTable('{table_id}', 14)">Current CPU Limits</th>
                <th onclick="sortTable('{table_id}', 15)">Timestamp</th>
            </tr>
        </thead>
        <tbody>
"""
    IO_HIGH_THRESHOLD_MBPS = 50.0  # highlight rows where I/O exceeds this value
    for exec_data in sorted(
        step_executions,
        key=lambda x: (
            x.get("cluster", ""),
            x.get("namespace", ""),
            x.get("timestamp", ""),
        ),
    ):
        app = exec_data.get("application", "N/A")
        mem_req = exec_data.get("mem_requests_k8s", "N/A")
        mem_lim = exec_data.get("mem_limits_k8s", "N/A")
        cpu_req = exec_data.get("cpu_requests_k8s", "N/A")
        cpu_lim = exec_data.get("cpu_limits_k8s", "N/A")
        io_read = exec_data.get("io_read_mbps", 0)
        io_write = exec_data.get("io_write_mbps", 0)
        io_class = (
            ' class="io-high"'
            if (io_read >= IO_HIGH_THRESHOLD_MBPS or io_write >= IO_HIGH_THRESHOLD_MBPS)
            else ""
        )
        html_content += f"""        <tr{io_class}>
            <td>{exec_data.get("cluster", "N/A")}</td>
            <td>{exec_data.get("namespace", "N/A")}</td>
            <td>{exec_data.get("task", "N/A")}</td>
            <td>{exec_data.get("step", "N/A")}</td>
            <td>{exec_data.get("component", "N/A")}</td>
            <td>{app}</td>
            <td>{exec_data.get("pod", "N/A")}</td>
            <td>{exec_data.get("memory_mb", 0)}</td>
            <td>{mem_req}</td>
            <td>{mem_lim}</td>
            <td>{exec_data.get("cpu_cores", 0)}</td>
            <td>{io_read}</td>
            <td>{io_write}</td>
            <td>{cpu_req}</td>
            <td>{cpu_lim}</td>
            <td>{exec_data.get("timestamp", "N/A")}</td>
        </tr>
"""
    html_content += """        </tbody>
    </table>
    </div>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    json_data = {
        "task_name": task_name,
        "step_name": step_name,
        "date": date_str,
        "timestamp": datetime.now().isoformat(),
        "executions_count": len(step_executions),
        "executions": step_executions,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    csv_header = (
        '"cluster","namespace","task","step","component","application","pod",'
        '"final_memory_mb","current_mem_requests","current_mem_limits",'
        '"final_cpu_cores","io_read_mbps","io_write_mbps",'
        '"current_cpu_requests","current_cpu_limits","timestamp"'
    )
    csv_lines = [csv_header]
    for exec_data in step_executions:
        csv_lines.append(
            f'"{exec_data.get("cluster", "")}",'
            f'"{exec_data.get("namespace", "")}",'
            f'"{exec_data.get("task", "")}",'
            f'"{exec_data.get("step", "")}",'
            f'"{exec_data.get("component", "N/A")}",'
            f'"{exec_data.get("application", "N/A")}",'
            f'"{exec_data.get("pod", "")}",'
            f"{exec_data.get('memory_mb', 0)},"
            f'"{exec_data.get("mem_requests_k8s", "N/A")}",'
            f'"{exec_data.get("mem_limits_k8s", "N/A")}",'
            f"{exec_data.get('cpu_cores', 0)},"
            f"{exec_data.get('io_read_mbps', 0)},"
            f"{exec_data.get('io_write_mbps', 0)},"
            f'"{exec_data.get("cpu_requests_k8s", "N/A")}",'
            f'"{exec_data.get("cpu_limits_k8s", "N/A")}",'
            f'"{exec_data.get("timestamp", "")}"'
        )
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("\n".join(csv_lines))

    return html_path, json_path, csv_path


def save_detailed_per_step_data(task_name, executions_data, date_str):
    """Save detailed per-step pod execution data as one HTML, JSON, and CSV per step.

    Filenames: {task}_analyzed_data_detailed_step_{step_name}_{date}[_{time}].html/json/csv

    Args:
        task_name: Task name
        executions_data: List of pod execution dictionaries
        date_str: Date string in YYYYMMDD format

    Returns:
        list of tuples: [(html_path, json_path, csv_path), ...] one per step
    """
    script_dir = Path(__file__).parent
    cache_dir = script_dir / ".analyze_cache"
    cache_dir.mkdir(exist_ok=True)
    safe_task_name = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)

    by_step = defaultdict(list)
    for exec_data in executions_data:
        step = exec_data.get("step", "")
        if step:
            by_step[step].append(exec_data)

    # Decide timestamp suffix: if any step's files already exist for this date, add timestamp
    timestamp_suffix = None
    for step_name in by_step:
        safe_step = re.sub(r"[^a-zA-Z0-9_-]", "_", step_name)
        base = f"{safe_task_name}_analyzed_data_detailed_step_{safe_step}_{date_str}"
        if (cache_dir / f"{base}.html").exists() or (cache_dir / f"{base}.json").exists():
            # Time only (date already in base) to avoid ..._20260217_20260217_180906
            timestamp_suffix = datetime.now().strftime("%H%M%S")
            break

    result_paths = []
    for step_name in sorted(by_step.keys()):
        step_executions = by_step[step_name]
        html_path, json_path, csv_path = _write_one_step_detailed_files(
            cache_dir, task_name, step_name, step_executions, date_str, timestamp_suffix
        )
        result_paths.append((html_path, json_path, csv_path))
    return result_paths


def split_existing_detailed_per_step_json_to_per_step_files(json_path):
    """One-time helper: read a combined detailed_per_step JSON and write one HTML/JSON/CSV per step.

    Args:
        json_path: Path to existing {task}_analyzed_data_detailed_per_step_{date}.json

    Returns:
        list of (html_path, json_path, csv_path) per step
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(json_path)
    cache_dir = path.parent
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    task_name = data.get("task_name", "")
    date_str = data.get("date", "")
    executions_by_step = data.get("executions_by_step", {})
    if not task_name or not date_str or not executions_by_step:
        raise ValueError("JSON missing task_name, date, or executions_by_step")
    result_paths = []
    for step_name in sorted(executions_by_step.keys()):
        step_executions = executions_by_step[step_name]
        html_path, json_path, csv_path = _write_one_step_detailed_files(
            cache_dir,
            task_name,
            step_name,
            step_executions,
            date_str,
            timestamp_suffix=None,
        )
        result_paths.append((html_path, json_path, csv_path))
    return result_paths
