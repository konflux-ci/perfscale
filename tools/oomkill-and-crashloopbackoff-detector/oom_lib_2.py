from __future__ import annotations

import csv
import glob
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from oom_constants import (
    BLUE,
    GREEN,
    RED,
    YELLOW,
)
from oom_lib_0 import color, report_generated_est, timestamp_for_backup_from_file
from oom_lib_3 import (
    _label_from_timestamped_csv_basename,
    _normalize_type,
    _run_date_from_timestamped_csv_basename,
    build_historical_series_by_cluster_from_output_dir,
    get_historical_html_links,
)

try:
    from html_export import generate_html_report
except ImportError:
    generate_html_report = None


def move_existing_output_files(target_dir: Path) -> int:
    """
    Move all existing output files (oom_results.* and timestamped versions)
    from current directory to target directory.

    Returns:
        Number of files moved
    """
    output_dir = target_dir
    # Ensure it exists (mkdir -p style: create if missing, never truncate existing)
    output_dir.mkdir(parents=True, exist_ok=True)
    moved_count = 0

    # Pattern to match output files
    output_patterns = [
        "oom_results.csv",
        "oom_results.json",
        "oom_results.html",
        "oom_results.table",
        "oom_results_*.csv",
        "oom_results_*.json",
        "oom_results_*.html",
        "oom_results_*.table",
    ]

    current_dir = Path(".")
    for pattern in output_patterns:
        # Handle wildcard patterns
        if "*" in pattern:
            files = glob.glob(str(current_dir / pattern))
        else:
            files = [str(current_dir / pattern)] if (current_dir / pattern).exists() else []

        for file_path_str in files:
            file_path = Path(file_path_str)
            if file_path.exists() and file_path.is_file():
                try:
                    dest_path = output_dir / file_path.name

                    # If source and destination are the same (e.g. output_dir is current dir), skip
                    if file_path.resolve() == dest_path.resolve():
                        continue

                    # If file already exists in output dir, skip (don't overwrite)
                    if not dest_path.exists():
                        file_path.rename(dest_path)
                        moved_count += 1
                    else:
                        # If destination exists, rename with timestamp
                        # from file's last modified time
                        timestamp = timestamp_for_backup_from_file(file_path)
                        suffix = file_path.suffix
                        stem = file_path.stem
                        backup_name = f"{stem}_{timestamp}{suffix}"
                        dest_path = output_dir / backup_name
                        file_path.rename(dest_path)
                        moved_count += 1
                except Exception as e:
                    logging.warning(f"Failed to move {file_path} to output directory: {e}")

    if moved_count > 0:
        print(color(f"Moved {moved_count} existing output file(s) to 'output' directory", YELLOW))

    return moved_count


def backup_existing_file(file_path: Path) -> Path | None:
    """Backup an existing file by renaming it with a timestamp.

    Args:
        file_path: Path to the file to backup

    Returns:
        Path to the backup file if backup was successful, None otherwise
    """
    if not file_path.exists():
        return None

    try:
        # Use file's last modified time for backup name (same format: DD-MMM-YYYY_HH-MM-SS-TZ)
        timestamp = timestamp_for_backup_from_file(file_path)
        suffix = file_path.suffix
        stem = file_path.stem
        backup_name = f"{stem}_{timestamp}{suffix}"
        backup_path = file_path.parent / backup_name

        # Rename the file
        file_path.rename(backup_path)
        return backup_path
    except Exception as e:
        logging.warning(f"Failed to backup {file_path}: {e}")
        return None


def backup_output_files(
    json_path: Path,
    csv_path: Path,
    table_path: Path,
    html_path: Path,
) -> None:
    """Backup existing output files before generating new ones."""
    backups = []

    for file_path in [json_path, csv_path, table_path, html_path]:
        backup_path = backup_existing_file(file_path)
        if backup_path:
            backups.append(backup_path)

    if backups:
        print(color(f"\nBacked up {len(backups)} existing file(s):", YELLOW))
        for backup_path in backups:
            print(color(f"  → {backup_path.name}", YELLOW))


def collect_rows(results: dict[str, Any], time_range_str: str = "1d") -> list[dict[str, str]]:
    """
    Collect all rows from results dictionary.

    Returns a list of dictionaries representing rows, sorted by type
    (OOMKilled first, then CrashLoopBackOff).
    """
    rows = []
    # Skip _metadata if present
    for cluster, ns_map in results.items():
        if cluster == "_metadata":
            continue
        for ns, pods in ns_map.items():
            for pod_name, info in pods.items():
                desc = info.get("description_file", "")
                plog = info.get("pod_log_file", "")
                sources = ";".join(info.get("sources", [])) if info.get("sources") else ""
                application = info.get("application", "")
                component = info.get("component", "")
                # OOM rows
                if info.get("oom_timestamps"):
                    rows.append(
                        {
                            "cluster": cluster,
                            "namespace": ns,
                            "pod": pod_name,
                            "type": "OOMKilled",
                            "application": application,
                            "component": component,
                            "timestamps": ";".join(info.get("oom_timestamps")),
                            "sources": sources,
                            "description_file": desc,
                            "pod_log_file": plog,
                            "time_range": time_range_str,
                        }
                    )
                # Crash rows
                if info.get("crash_timestamps"):
                    rows.append(
                        {
                            "cluster": cluster,
                            "namespace": ns,
                            "pod": pod_name,
                            "type": "CrashLoopBackOff",
                            "application": application,
                            "component": component,
                            "timestamps": ";".join(info.get("crash_timestamps")),
                            "sources": sources,
                            "description_file": desc,
                            "pod_log_file": plog,
                            "time_range": time_range_str,
                        }
                    )

    # Sort: OOMKilled first, then CrashLoopBackOff
    def sort_key(row: dict[str, str]) -> tuple[int, str, str, str]:
        type_val = row.get("type", "")
        if type_val == "OOMKilled":
            return (
                0,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )
        elif type_val == "CrashLoopBackOff":
            return (
                1,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )
        else:
            return (
                2,
                row.get("cluster", ""),
                row.get("namespace", ""),
                row.get("pod", ""),
            )

    rows.sort(key=sort_key)
    return rows


def export_table(rows: list[dict[str, str]], table_path: Path) -> None:
    """Export rows to a table-formatted file."""
    if not rows:
        return

    columns = [
        "cluster",
        "namespace",
        "pod",
        "type",
        "application",
        "component",
        "timestamps",
        "sources",
        "description_file",
        "pod_log_file",
        "time_range",
    ]

    # Calculate column widths
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(row.get(col, "")))

    # Generate table
    lines = []

    # Build header row first to calculate exact width
    header_parts = [f" {col:<{widths[col]}} " for col in columns]
    header_row = "|" + "|".join(header_parts) + "|"

    # Calculate total width: length of the header row
    total_width = len(header_row)

    # Header separator (continuous line of dashes matching table width)
    header_sep = "-" * total_width
    lines.append(header_sep)

    # Header row
    lines.append(header_row)

    # Header separator again
    lines.append(header_sep)

    # Data rows
    for row in rows:
        data_parts = [f" {row.get(col, ''):<{widths[col]}} " for col in columns]
        data_row = "|" + "|".join(data_parts) + "|"
        lines.append(data_row)

    # Footer separator
    lines.append(header_sep)

    # Write to file
    try:
        table_path.write_text("\n".join(lines))
        print(color(f"Table written → {table_path}", GREEN))
    except OSError as e:
        logging.error(f"Failed to write table file {table_path}: {e}")
        print(color(f"ERROR: Failed to write table file: {e}", RED))


def export_results(
    results: dict[str, Any],
    json_path: Path,
    csv_path: Path,
    table_path: Path,
    html_path: Path | None = None,
    time_range_str: str = "1d",
    output_dir: Path | None = None,
    plot_range_seconds: int | None = None,
    plot_range_str: str = "2M",
) -> None:
    """Export results to JSON, CSV, TABLE, and HTML files."""
    # Collect and sort rows
    rows = collect_rows(results, time_range_str)

    # Export JSON
    results_with_metadata = results.copy()
    results_with_metadata["_metadata"] = {"time_range": time_range_str}
    try:
        json_path.write_text(json.dumps(results_with_metadata, indent=2))
        print(color(f"JSON written → {json_path}", GREEN))
    except OSError as e:
        logging.error(f"Failed to write JSON file {json_path}: {e}")
        print(color(f"ERROR: Failed to write JSON file: {e}", RED))

    # Export CSV
    try:
        with csv_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "cluster",
                    "namespace",
                    "pod",
                    "type",
                    "application",
                    "component",
                    "timestamps",
                    "sources",
                    "description_file",
                    "pod_log_file",
                    "time_range",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["cluster"],
                        row["namespace"],
                        row["pod"],
                        row["type"],
                        row.get("application", ""),
                        row.get("component", ""),
                        row["timestamps"],
                        row["sources"],
                        row["description_file"],
                        row["pod_log_file"],
                        row["time_range"],
                    ]
                )
        print(color(f"CSV written → {csv_path}", GREEN))
    except OSError as e:
        logging.error(f"Failed to write CSV file {csv_path}: {e}")
        print(color(f"ERROR: Failed to write CSV file: {e}", RED))

    # Export TABLE
    export_table(rows, table_path)

    # Export HTML (with optional historical graph)
    if html_path and generate_html_report:
        try:
            historical_series = None
            historical_series_by_cluster = {}
            historical_html_links = []
            if output_dir is not None and plot_range_seconds is not None:
                historical_series = build_historical_series_from_output_dir(
                    output_dir, plot_range_seconds
                )
                historical_series_by_cluster = build_historical_series_by_cluster_from_output_dir(
                    output_dir, plot_range_seconds
                )
            if output_dir is not None:
                historical_html_links = get_historical_html_links(output_dir)
            generate_html_report(
                rows,
                time_range_str,
                html_path,
                report_generated_est=report_generated_est(),
                historical_series=historical_series,
                historical_series_by_cluster=historical_series_by_cluster,
                historical_html_links=historical_html_links,
                plot_range_str=plot_range_str,
            )
            print(color(f"HTML written → {html_path}", GREEN))
        except Exception as e:
            logging.error(f"Failed to write HTML file {html_path}: {e}")
            print(color(f"ERROR: Failed to write HTML file: {e}", RED))
    elif html_path and not generate_html_report:
        logging.warning("HTML export module not available, skipping HTML generation")
        print(color("WARNING: HTML export module not available, skipping HTML generation", YELLOW))


def pretty_print(results: dict[str, Any], skipped: dict[str, str]) -> None:
    for cluster, ns_map in results.items():
        print()
        print(color(f"Cluster: {cluster}", BLUE))
        if not ns_map:
            print(color("  (no namespaces with OOM/CrashLoopBackOff found)", GREEN))
            continue
        for ns, pods in ns_map.items():
            print(color(f"  Namespace: {ns}", YELLOW))
            for pod_name, info in pods.items():
                heading_color = (
                    RED if (info.get("oom_timestamps") or info.get("crash_timestamps")) else GREEN
                )
                print(color(f"    Pod: {pod_name}", heading_color))
                if info.get("oom_timestamps"):
                    for t in info["oom_timestamps"]:
                        print(f"      - OOMKilled at: {t}")
                if info.get("crash_timestamps"):
                    for t in info["crash_timestamps"]:
                        print(f"      - CrashLoopBackOff event at: {t}")
                if not info.get("oom_timestamps") and not info.get("crash_timestamps"):
                    sources_str = ", ".join(info.get("sources", []))
                    print(f"      - Detected (no timestamps) via sources: {sources_str}")
                # print artifacts paths
                if info.get("description_file") or info.get("pod_log_file"):
                    print(f"      - description_file: {info.get('description_file', '')}")
                    print(f"      - pod_log_file: {info.get('pod_log_file', '')}")
    if skipped:
        print()
        print(color("Skipped / Unreachable clusters:", RED))
        for c, msg in skipped.items():
            print(color(f"  {c}: {msg}", RED))


def build_historical_series_from_output_dir(
    output_dir: Path,
    plot_range_seconds: int,
) -> list[tuple[str, int, int]]:
    """
    Build historical (label, oom_count, crash_count) from timestamped CSVs in output_dir,
    plus the current run from oom_results.csv if present (so today's run appears on the graph).
    Uses run date from filename (DD-Mon-YYYY); fallback to file mtime date if parse fails.
    Cutoff is relative to the **latest run in the directory** (not "now"). Sorted by run date.
    """
    resolved_dir = output_dir.resolve()
    series: list[tuple[str, int, int, date]] = []
    # 1) Timestamped backup CSVs
    files = sorted(resolved_dir.glob("oom_results_*_*.csv"))
    for path in files:
        try:
            run_date = _run_date_from_timestamped_csv_basename(path.name)
            if run_date is None:
                run_date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
            label = _label_from_timestamped_csv_basename(path.name) or path.name
            oom, crash = 0, 0
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = _normalize_type(row.get("type", ""))
                    if t == "OOMKilled":
                        oom += 1
                    elif t == "CrashLoopBackOff":
                        crash += 1
            series.append((label, oom, crash, run_date))
        except (OSError, csv.Error) as e:
            logging.debug(f"Skip {path.name}: {e}")
            continue
    # 2) Current run (oom_results.csv) so today's run appears on the graph
    main_csv = resolved_dir / "oom_results.csv"
    if main_csv.is_file():
        try:
            mtime = main_csv.stat().st_mtime
            run_date = datetime.fromtimestamp(mtime, tz=UTC).date()
            label = datetime.fromtimestamp(mtime).strftime(
                "%d-%b-%Y %H:%M"
            )  # local time for display
            oom, crash = 0, 0
            with main_csv.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = _normalize_type(row.get("type", ""))
                    if t == "OOMKilled":
                        oom += 1
                    elif t == "CrashLoopBackOff":
                        crash += 1
            series.append((label, oom, crash, run_date))
        except (OSError, csv.Error) as e:
            logging.debug(f"Skip {main_csv.name}: {e}")
    if not series:
        return []
    # Cutoff relative to latest run in this directory (avoids dependence on system clock)
    latest = max(run_d for (_, _, _, run_d) in series)
    cutoff_ts = (
        datetime.combine(latest, datetime.min.time()).replace(tzinfo=UTC).timestamp()
        - plot_range_seconds
    )
    cutoff_date = datetime.fromtimestamp(cutoff_ts, tz=UTC).date()
    series = [
        (label, oom, crash, run_d) for (label, oom, crash, run_d) in series if run_d >= cutoff_date
    ]
    series.sort(key=lambda x: (x[3], x[0]))
    return [(label, oom, crash) for label, oom, crash, _ in series]
