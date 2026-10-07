#!/usr/bin/env python3
"""
html_export.py

HTML export module for OOMKilled / CrashLoopBackOff detector.
Generates a standalone HTML report that can be opened directly in a browser.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from html_assets import _get_css_styles, _get_sorting_javascript


def _svg_single_series_chart(
    labels: list[str],
    counts: list[int],
    color_hex: str,
    stroke_width: int = 2,
    width: int = 800,
    height: int = 388,
    vertical_x_labels: bool = True,
) -> str:
    """Generate an inline SVG line chart for one series (own Y scale).

    X-axis labels are drawn vertically so all dates fit;
    chart width can exceed 800px for many points.
    """
    if not labels:
        return '<p class="chart-empty">No historical data points.</p>'
    n = len(labels)
    pad_left = 56
    pad_right = 24
    pad_top = 24
    pad_bottom = 120  # room for vertical X labels (readable below axis)
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    max_val = max(max(counts or [0]), 1)
    y_max = max_val if max_val <= 10 else (max_val + 1)

    def y_pos(val: float) -> float:
        return pad_top + plot_h - (val / y_max) * plot_h if y_max else pad_top + plot_h

    def x_pos(i: int) -> float:
        if n <= 1:
            return pad_left + plot_w / 2
        return pad_left + (i / (n - 1)) * plot_w

    pts = " ".join(f"{x_pos(i)},{y_pos(counts[i] if i < len(counts) else 0)}" for i in range(n))
    r = 4
    circles = "".join(
        f'<circle cx="{x_pos(i)}" cy="{y_pos(counts[i] if i < len(counts) else 0)}"'
        f' r="{r}" fill="{color_hex}" stroke="{color_hex}" stroke-width="1"/>'
        for i in range(n)
    )
    # Value labels just above each dot
    value_labels = "".join(
        f'<text x="{x_pos(i)}"'
        f' y="{y_pos(counts[i] if i < len(counts) else 0) - 10}"'
        f' text-anchor="middle" class="chart-value-label"'
        f' font-size="10" fill="#374151">'
        f"{counts[i] if i < len(counts) else 0}</text>"
        for i in range(n)
    )
    # Vertical X labels: start just below the axis (rotate 90,
    # text extends down); keep fully inside SVG
    axis_y = pad_top + plot_h  # = height - pad_bottom
    label_y = axis_y + 8  # 8px below axis so labels sit close to x-axis
    x_ticks = []
    step = 1 if vertical_x_labels else max(1, (n + 9) // 10)
    for i in range(0, n, step):
        x = x_pos(i)
        label = labels[i] if i < len(labels) else ""
        short = label if len(label) <= 14 else label[:12] + ".."
        if vertical_x_labels:
            x_ticks.append(
                f'<text x="{x}" y="{label_y}" text-anchor="start"'
                f' class="chart-x-label chart-x-label-vertical"'
                f' font-size="10"'
                f' transform="rotate(90, {x}, {label_y})">'
                f"{escape_html(short)}</text>"
            )
        else:
            x_ticks.append(
                f'<text x="{x}" y="{label_y}"'
                f' text-anchor="middle" class="chart-x-label"'
                f' font-size="10">{escape_html(short)}</text>'
            )
    x_ticks_html = "\n                ".join(x_ticks)
    y_ticks_html_parts = []
    step_y = max(1, int(y_max) // 8) if y_max >= 8 else 1
    for v in range(0, int(y_max) + 1, step_y):
        y = y_pos(v)
        y_ticks_html_parts.append(
            f'<text x="{pad_left - 6}" y="{y + 4}"'
            f' text-anchor="end" class="chart-y-label"'
            f' font-size="10">{v}</text>'
            f'<line x1="{pad_left}" y1="{y}"'
            f' x2="{pad_left + plot_w}" y2="{y}"'
            f' stroke="#e5e7eb" stroke-dasharray="2,2"/>'
        )
    y_ticks_html = "\n                ".join(y_ticks_html_parts)
    svg_header = (
        f'<svg class="inline-chart-svg"'
        f' viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}"'
        f' preserveAspectRatio="xMinYMid meet"'
        f' style="overflow: visible;">'
    )
    y_axis = (
        f'<line x1="{pad_left}" y1="{pad_top}"'
        f' x2="{pad_left}" y2="{pad_top + plot_h}"'
        f' stroke="#374151" stroke-width="1"/>'
    )
    x_axis = (
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}"'
        f' x2="{pad_left + plot_w}" y2="{pad_top + plot_h}"'
        f' stroke="#374151" stroke-width="1"/>'
    )
    polyline = (
        f'<polyline points="{pts}" fill="none"'
        f' stroke="{color_hex}"'
        f' stroke-width="{stroke_width}"'
        f' stroke-linejoin="round"'
        f' stroke-linecap="round"/>'
    )
    svg = f"""{svg_header}
            {y_axis}
            {x_axis}
            {y_ticks_html}
            {x_ticks_html}
            {polyline}
            {circles}
            {value_labels}
        </svg>"""
    return svg


def _svg_dual_series_chart(
    labels: list[str],
    oom_counts: list[int],
    crash_counts: list[int],
    width: int = 800,
    height: int = 388,
    vertical_x_labels: bool = True,
) -> str:
    """Generate an inline SVG line chart with two series.

    OOM red, CrashLoop blue on one Y scale.
    Same layout as single-series: vertical X labels,
    scrollable when wide.
    """
    if not labels:
        return '<p class="chart-empty">No historical data points.</p>'
    n = len(labels)
    pad_left = 56
    pad_right = 24
    pad_top = 24
    pad_bottom = 120
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    max_val = max(
        max(oom_counts or [0]),
        max(crash_counts or [0]),
        1,
    )
    y_max = max_val if max_val <= 10 else (max_val + 1)

    def y_pos(val: float) -> float:
        return pad_top + plot_h - (val / y_max) * plot_h if y_max else pad_top + plot_h

    def x_pos(i: int) -> float:
        if n <= 1:
            return pad_left + plot_w / 2
        return pad_left + (i / (n - 1)) * plot_w

    oom_pts = " ".join(
        f"{x_pos(i)},{y_pos(oom_counts[i] if i < len(oom_counts) else 0)}" for i in range(n)
    )
    crash_pts = " ".join(
        f"{x_pos(i)},{y_pos(crash_counts[i] if i < len(crash_counts) else 0)}" for i in range(n)
    )
    r = 4
    oom_circles = "".join(
        f'<circle cx="{x_pos(i)}"'
        f' cy="{y_pos(oom_counts[i] if i < len(oom_counts) else 0)}"'
        f' r="{r}" fill="#b91c1c" stroke="#b91c1c"'
        f' stroke-width="1"/>'
        for i in range(n)
    )
    crash_circles = "".join(
        f'<circle cx="{x_pos(i)}"'
        f' cy="{y_pos(crash_counts[i] if i < len(crash_counts) else 0)}"'
        f' r="{r}" fill="#1d4ed8" stroke="#1d4ed8"'
        f' stroke-width="1"/>'
        for i in range(n)
    )
    oom_values = "".join(
        f'<text x="{x_pos(i)}"'
        f' y="{y_pos(oom_counts[i] if i < len(oom_counts) else 0) - 10}"'
        f' text-anchor="middle" class="chart-value-label"'
        f' font-size="9" fill="#b91c1c">'
        f"{oom_counts[i] if i < len(oom_counts) else 0}</text>"
        for i in range(n)
    )
    crash_values = "".join(
        f'<text x="{x_pos(i)}"'
        f' y="{y_pos(crash_counts[i] if i < len(crash_counts) else 0) + 14}"'
        f' text-anchor="middle" class="chart-value-label"'
        f' font-size="9" fill="#1d4ed8">'
        f"{crash_counts[i] if i < len(crash_counts) else 0}</text>"
        for i in range(n)
    )
    axis_y = pad_top + plot_h
    label_y = axis_y + 8
    x_ticks = []
    step = 1 if vertical_x_labels else max(1, (n + 9) // 10)
    for i in range(0, n, step):
        x = x_pos(i)
        label = labels[i] if i < len(labels) else ""
        short = label if len(label) <= 14 else label[:12] + ".."
        if vertical_x_labels:
            x_ticks.append(
                f'<text x="{x}" y="{label_y}" text-anchor="start"'
                f' class="chart-x-label chart-x-label-vertical"'
                f' font-size="10"'
                f' transform="rotate(90, {x}, {label_y})">'
                f"{escape_html(short)}</text>"
            )
        else:
            x_ticks.append(
                f'<text x="{x}" y="{label_y}"'
                f' text-anchor="middle" class="chart-x-label"'
                f' font-size="10">{escape_html(short)}</text>'
            )
    x_ticks_html = "\n                ".join(x_ticks)
    y_ticks_html_parts = []
    step_y = max(1, int(y_max) // 8) if y_max >= 8 else 1
    for v in range(0, int(y_max) + 1, step_y):
        y = y_pos(v)
        y_ticks_html_parts.append(
            f'<text x="{pad_left - 6}" y="{y + 4}"'
            f' text-anchor="end" class="chart-y-label"'
            f' font-size="10">{v}</text>'
            f'<line x1="{pad_left}" y1="{y}"'
            f' x2="{pad_left + plot_w}" y2="{y}"'
            f' stroke="#e5e7eb" stroke-dasharray="2,2"/>'
        )
    y_ticks_html = "\n                ".join(y_ticks_html_parts)
    svg_header = (
        f'<svg class="inline-chart-svg"'
        f' viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}"'
        f' preserveAspectRatio="xMinYMid meet"'
        f' style="overflow: visible;">'
    )
    y_axis = (
        f'<line x1="{pad_left}" y1="{pad_top}"'
        f' x2="{pad_left}" y2="{pad_top + plot_h}"'
        f' stroke="#374151" stroke-width="1"/>'
    )
    x_axis = (
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}"'
        f' x2="{pad_left + plot_w}" y2="{pad_top + plot_h}"'
        f' stroke="#374151" stroke-width="1"/>'
    )
    oom_polyline = (
        f'<polyline points="{oom_pts}" fill="none"'
        f' stroke="#b91c1c" stroke-width="3"'
        f' stroke-linejoin="round" stroke-linecap="round"/>'
    )
    crash_polyline = (
        f'<polyline points="{crash_pts}" fill="none"'
        f' stroke="#1d4ed8" stroke-width="2"'
        f' stroke-linejoin="round" stroke-linecap="round"/>'
    )
    oom_legend = (
        f'<text x="{pad_left + plot_w + 8}"'
        f' y="{pad_top + 12}" class="chart-legend"'
        f' font-size="11" fill="#b91c1c"'
        f' font-weight="bold">OOMKilled</text>'
    )
    crash_legend = (
        f'<text x="{pad_left + plot_w + 8}"'
        f' y="{pad_top + 28}" class="chart-legend"'
        f' font-size="11" fill="#1d4ed8">'
        f"CrashLoopBackOff</text>"
    )
    svg = f"""{svg_header}
            {y_axis}
            {x_axis}
            {y_ticks_html}
            {x_ticks_html}
            {oom_polyline}
            {crash_polyline}
            {oom_circles}
            {crash_circles}
            {oom_values}
            {crash_values}
            {oom_legend}
            {crash_legend}
        </svg>"""
    return svg


def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text))


def _plot_range_to_readable(plot_range_str: str | None) -> str:
    """Convert plot range abbreviation to readable form.

    E.g. 2M -> '2 months', 7d -> '7 days'.
    """
    if not plot_range_str or not str(plot_range_str).strip():
        return "2 months"
    s = str(plot_range_str).strip()
    m = re.match(r"^(\d+)([smhdM])$", s)
    if not m:
        return s
    value = int(m.group(1))
    unit = m.group(2)
    units = {
        "s": ("second", "seconds"),
        "m": ("minute", "minutes"),
        "h": ("hour", "hours"),
        "d": ("day", "days"),
        "M": ("month", "months"),
    }
    singular, plural = units.get(unit, ("", ""))
    if not singular:
        return s
    return f"{value} {singular}" if value == 1 else f"{value} {plural}"


def generate_html_report(
    rows: list[dict[str, str]],
    time_range_str: str,
    html_path: Path,
    report_generated_est: str | None = None,
    historical_series: list[tuple[str, int, int]] | None = None,
    historical_series_by_cluster: dict[str, list[tuple[str, int, int]]] | None = None,
    historical_html_links: list[tuple[str, str]] | None = None,
    plot_range_str: str | None = None,
) -> None:
    """
    Generate a standalone HTML report from collected rows.

    Args:
        rows: List of dictionaries representing OOM/CrashLoopBackOff findings
        time_range_str: Time range string used for detection (e.g., "1d", "6h")
        html_path: Path where HTML file should be written
        report_generated_est: Report generated timestamp (e.g. EST) for header
        historical_series: List of (label, oom_count, crash_count) for all-clusters graph
        historical_series_by_cluster: Per-cluster (label, oom, crash) for per-cluster graphs
        historical_html_links: List of (label, filename) for links to past HTML reports
        plot_range_str: Label for plot range (e.g. "2M") for graph subtitle
    """
    if not rows and not historical_series:
        html_content = _generate_empty_html(time_range_str, report_generated_est)
    else:
        html_content = _generate_html_with_data(
            rows or [],
            time_range_str,
            report_generated_est=report_generated_est,
            historical_series=historical_series,
            historical_series_by_cluster=historical_series_by_cluster or {},
            historical_html_links=historical_html_links or [],
            plot_range_str=plot_range_str,
        )
    try:
        html_path.write_text(html_content, encoding="utf-8")
    except OSError as e:
        raise OSError(f"Failed to write HTML file {html_path}: {e}") from e


def _generate_empty_html(time_range_str: str, report_generated_est: str | None = None) -> str:
    """Generate HTML for empty results."""
    title_text = "OOM / CrashLoopBackOff Detection Report"
    if report_generated_est:
        title_text += f" ({escape_html(report_generated_est)})"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OOM / CrashLoopBackOff Report - No Issues Found</title>
    <style>
        {_get_css_styles()}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="report-org-line">- Performance and Scale Engineering -</div>
            <h1>{title_text}</h1>
            <div class="metadata">
                <span class="badge badge-success">No Issues Found</span>
                <span class="badge badge-info">Time Range: {escape_html(time_range_str)}</span>
            </div>
        </header>
        <main>
            <div class="empty-state">
                <p>No OOMKilled or CrashLoopBackOff pods
                detected in the specified time range.</p>
            </div>
        </main>
    </div>
</body>
</html>"""


def _generate_html_with_data(
    rows: list[dict[str, str]],
    time_range_str: str,
    report_generated_est: str | None = None,
    historical_series: list[tuple[str, int, int]] | None = None,
    historical_series_by_cluster: dict[str, list[tuple[str, int, int]]] | None = None,
    historical_html_links: list[tuple[str, str]] | None = None,
    plot_range_str: str | None = None,
) -> str:
    """Generate HTML report with data.

    Order: header, graphs, Summary, Detailed Findings,
    Historical report links.
    """
    total_findings = len(rows)
    oom_count = sum(1 for r in rows if r.get("type") == "OOMKilled")
    crash_count = sum(1 for r in rows if r.get("type") == "CrashLoopBackOff")

    title_text = "OOM / CrashLoopBackOff Detection Report"
    if report_generated_est:
        title_text += f" ({escape_html(report_generated_est)})"

    plot_label = escape_html(_plot_range_to_readable(plot_range_str or "2M"))
    graph_section = ""
    if historical_series:
        labels = [s[0] for s in historical_series]
        oom_counts = [s[1] for s in historical_series]
        crash_counts = [s[2] for s in historical_series]
        n = len(labels)
        chart_width = max(800, n * 50)
        chart_height = 388
        oom_svg = _svg_single_series_chart(
            labels,
            oom_counts,
            color_hex="#b91c1c",
            stroke_width=3,
            width=chart_width,
            height=chart_height,
            vertical_x_labels=True,
        )
        crash_svg = _svg_single_series_chart(
            labels,
            crash_counts,
            color_hex="#1d4ed8",
            stroke_width=2,
            width=chart_width,
            height=chart_height,
            vertical_x_labels=True,
        )
        data_table_rows = "".join(
            f"<tr><td>{escape_html(lb)}</td>"
            f'<td class="number">{o}</td>'
            f'<td class="number">{c}</td></tr>'
            for lb, o, c in historical_series
        )
        oom_heading = (
            f"OOM - Historical trend (all Konflux clusters) &mdash; Plot range: {plot_label}"
        )
        crash_heading = (
            "CrashLoopBackOffs - Historical trend"
            " (all Konflux clusters)"
            f" &mdash; Plot range: {plot_label}"
        )
        table_heading = (
            "Table of total OOMs &amp; CrashLoopBackOffs -"
            " Historical trend (All clusters)"
            f" &mdash; Plot range: {plot_label}"
        )
        thead = (
            "<thead><tr>"
            "<th>Date (run)</th>"
            '<th class="number">OOMKilled</th>'
            '<th class="number">CrashLoopBackOff</th>'
            "</tr></thead>"
        )
        graph_section = f"""
        <section class="graph-section">
            <h2>{oom_heading}</h2>
            <div class="chart-container chart-container-svg chart-scroll-wrap">
                {oom_svg}
            </div>
            <h2>{crash_heading}</h2>
            <div class="chart-container chart-container-svg chart-scroll-wrap">
                {crash_svg}
            </div>
            <h2>{table_heading}</h2>
            <table class="summary-table historical-fallback-table">
                {thead}
                <tbody>{data_table_rows}</tbody>
            </table>
        </section>"""

    # Per-cluster graphs: both OOM and CrashLoop in one chart per cluster, sorted by total (desc)
    per_cluster_section = ""
    if historical_series_by_cluster:
        # Sort clusters by total (oom+crash) across all points, descending
        def cluster_total(cluster_name: str) -> int:
            series = historical_series_by_cluster.get(cluster_name, [])
            return sum(o + c for (_, o, c) in series)

        clusters_sorted = sorted(
            historical_series_by_cluster.keys(),
            key=cluster_total,
            reverse=True,
        )
        parts = []
        for cluster_name in clusters_sorted:
            series = historical_series_by_cluster[cluster_name]
            if not series:
                continue
            labels_c = [s[0] for s in series]
            oom_c = [s[1] for s in series]
            crash_c = [s[2] for s in series]
            n_c = len(labels_c)
            chart_width_c = max(800, n_c * 50)
            chart_height_c = 388
            dual_svg = _svg_dual_series_chart(
                labels_c,
                oom_c,
                crash_c,
                width=chart_width_c,
                height=chart_height_c,
                vertical_x_labels=True,
            )
            cluster_esc = escape_html(cluster_name)
            cluster_h = (
                "OOM &amp; CrashLoopBackOffs - Historical trend"
                f" (cluster: {cluster_esc})"
                f" &mdash; Plot range: {plot_label}"
            )
            parts.append(f"""
            <h2>{cluster_h}</h2>
            <div class="chart-container chart-container-svg chart-scroll-wrap">
                {dual_svg}
            </div>""")
        if parts:
            per_cluster_section = (
                """
        <section class="graph-section graph-section-per-cluster">
            """
                + "\n".join(parts)
                + """
        </section>"""
            )

    summary_table = _generate_summary_table(rows)
    details_table = _generate_details_table(rows)

    # Historical report links (past timestamped HTML files)
    historical_reports_section = ""
    if historical_html_links:
        link_rows = "".join(
            f"<tr><td>{escape_html(label)}</td>"
            f'<td><a href="{escape_html(filename)}"'
            f' class="file-link">Open report</a></td></tr>'
            for label, filename in historical_html_links
        )
        historical_reports_section = f"""
            <section class="historical-reports-section">
                <h2>Historical HTML reports</h2>
                <table class="summary-table">
                    <thead><tr><th>Date (run)</th><th>Report</th></tr></thead>
                    <tbody>{link_rows}</tbody>
                </table>
            </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OOM / CrashLoopBackOff Report</title>
    <style>
        {_get_css_styles()}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="report-org-line">- Performance and Scale Engineering -</div>
            <h1>{title_text}</h1>
            <div class="metadata">
                <span class="badge badge-danger">Total Findings: {total_findings}</span>
                <span class="badge badge-warning">OOMKilled: {oom_count}</span>
                <span class="badge badge-warning">CrashLoopBackOff: {crash_count}</span>
                <span class="badge badge-info">Time Range: {escape_html(time_range_str)}</span>
            </div>
        </header>
        <main>
            {graph_section}
            {per_cluster_section}
            <section class="summary-section">
                <h2>Clusterwise Summary ({escape_html(report_generated_est or "N/A")})</h2>
                {summary_table}
            </section>
            <section class="details-section">
                <h2>PODs, Namespaces &amp; Clusters Detailed Findings\
 ({escape_html(report_generated_est or "N/A")})</h2>
                <div class="details-table-wrap">
                    {details_table}
                </div>
            </section>
            {historical_reports_section}
        </main>
        <footer>
            <p>Report generated by oc_get_ooms.py</p>
        </footer>
    </div>
    <script>
        {_get_sorting_javascript()}
    </script>
</body>
</html>"""


def _generate_summary_table(rows: list[dict[str, str]]) -> str:
    """Generate summary statistics table."""
    # Count by cluster and type
    cluster_stats = {}
    for row in rows:
        cluster = row.get("cluster", "unknown")
        issue_type = row.get("type", "unknown")
        if cluster not in cluster_stats:
            cluster_stats[cluster] = {"OOMKilled": 0, "CrashLoopBackOff": 0}
        cluster_stats[cluster][issue_type] = cluster_stats[cluster].get(issue_type, 0) + 1

    table_rows = []
    for cluster in sorted(cluster_stats.keys()):
        stats = cluster_stats[cluster]
        total = stats["OOMKilled"] + stats["CrashLoopBackOff"]
        table_rows.append(f"""
            <tr>
                <td>{escape_html(cluster)}</td>
                <td class="number">{stats["OOMKilled"]}</td>
                <td class="number">{stats["CrashLoopBackOff"]}</td>
                <td class="number"><strong>{total}</strong></td>
            </tr>""")

    return f"""
        <table class="summary-table">
            <thead>
                <tr>
                    <th>Cluster</th>
                    <th>OOMKilled</th>
                    <th>CrashLoopBackOff</th>
                    <th>Total</th>
                </tr>
            </thead>
            <tbody>
                {"".join(table_rows)}
            </tbody>
        </table>"""


def _generate_details_table(rows: list[dict[str, str]]) -> str:
    """Generate detailed findings table matching oom_results.table format."""
    table_rows = []
    for row in rows:
        cluster = escape_html(row.get("cluster", ""))
        namespace = escape_html(row.get("namespace", ""))
        pod = escape_html(row.get("pod", ""))
        issue_type = row.get("type", "")
        application = escape_html(row.get("application", ""))
        component = escape_html(row.get("component", ""))
        timestamps = escape_html(row.get("timestamps", ""))
        sources = escape_html(row.get("sources", ""))
        desc_file = escape_html(row.get("description_file", ""))
        log_file = escape_html(row.get("pod_log_file", ""))
        time_range = escape_html(row.get("time_range", ""))

        # Type badge
        type_class = "badge-oom" if issue_type == "OOMKilled" else "badge-crash"
        type_badge = f'<span class="badge {type_class}">{escape_html(issue_type)}</span>'

        # File links
        desc_link = (
            f'<a href="file://{desc_file}" class="file-link" title="{desc_file}">View</a>'
            if desc_file
            else "<em>N/A</em>"
        )
        log_link = (
            f'<a href="file://{log_file}" class="file-link" title="{log_file}">View</a>'
            if log_file
            else "<em>N/A</em>"
        )

        table_rows.append(f"""
            <tr>
                <td>{cluster}</td>
                <td>{namespace}</td>
                <td class="pod-name">{pod}</td>
                <td class="pod-type">{type_badge}</td>
                <td class="pod-application">{application}</td>
                <td class="pod-component">{component}</td>
                <td class="pod-timestamps">{timestamps}</td>
                <td class="pod-sources">{sources}</td>
                <td class="pod-files">{desc_link}</td>
                <td class="pod-files">{log_link}</td>
                <td>{time_range}</td>
            </tr>""")

    col_names = [
        "Cluster",
        "Namespace",
        "Pod",
        "Type",
        "Application",
        "Component",
        "Timestamps",
        "Sources",
        "Description File",
        "Pod Log File",
        "Time Range",
    ]
    th = '<span class="sort-indicator">↕</span>'
    header_cells = "\n                    ".join(
        f'<th class="sortable-header" data-sort="text">{name} {th}</th>' for name in col_names
    )
    return f"""
        <table class="details-table sortable">
            <thead>
                <tr>
                    {header_cells}
                </tr>
            </thead>
            <tbody>
                {"".join(table_rows)}
            </tbody>
        </table>"""
