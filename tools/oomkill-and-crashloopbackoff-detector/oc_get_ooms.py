#!/usr/bin/env python3
"""
oc_get_ooms.py

Detect OOMKilled and CrashLoopBackOff pods across multiple OpenShift/Kubernetes contexts,
parallelized at cluster and namespace levels, with artifact collection.

New in this version:
- When a pod is detected as OOMKilled or CrashLoopBackOff, save:
    - `oc describe pod <pod>` output
    - One log file with `oc logs <pod> --previous` (crashed container)
      then `oc logs <pod>` (current), appended
  into per-cluster directories under output/logs_and_description_files/<cluster>/
  Filenames include namespace, pod name, and timestamp to avoid collisions.
- CSV and JSON now include the absolute paths to the description and pod log files:
    description_file, pod_log_file

All previously requested features retained:
- cluster parallelism, namespace batching, include/exclude regex, retries, timeouts,
  time range filtering, colorized output, etc.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from re import Pattern

from oom_constants import (
    _EXCLUDE_PATTERNS,
    _INCLUDE_PATTERNS,
    BLUE,
    DEFAULT_BATCH_SIZE,
    DEFAULT_NS_BATCH_SIZE,
    DEFAULT_NS_WORKERS,
    DEFAULT_OC_TIMEOUT,
    DEFAULT_RETRIES,
    GREEN,
    RED,
    YELLOW,
)

try:
    from html_export import generate_html_report
except ImportError:
    generate_html_report = None

from oom_lib_0 import (
    check_all_clusters_connectivity,
    color,
    crashloop_via_pods_oc,
    get_all_contexts,
    get_all_events_oc,
    get_current_context,
    get_pods_items,
    match_contexts_by_substring,
    oomkilled_via_pods_oc,
    parse_time_range,
    print_connectivity_report_summary,
    report_generated_est,
    short_cluster_name,
)
from oom_lib_1 import (
    ensure_output_directory,
    get_namespaces_for_context,
    namespace_worker_oc,
    run_batches,
)
from oom_lib_2 import (
    backup_output_files,
    build_historical_series_from_output_dir,
    collect_rows,
    export_results,
    move_existing_output_files,
    pretty_print,
)
from oom_lib_3 import (
    _match_string_for_bundle_generator,
    _pod_base_name,
    _read_csv_rows_with_date,
    build_historical_series_by_cluster_from_output_dir,
    get_historical_html_links,
    print_per_pod_summary,
    resolve_codeowners_dir,
)

# Re-export symbols that tests patch via oc_get_ooms.*
__all__ = [
    "crashloop_via_pods_oc",
    "get_all_events_oc",
    "get_pods_items",
    "namespace_worker_oc",
    "oomkilled_via_pods_oc",
]


def print_usage_and_exit() -> None:
    print(
        """
Usage:
  oc_get_ooms.py [OPTIONS]

Context Selection (choose one):
  --current                Run only on current-context
  --contexts ctxA,ctxB     Comma-separated context substrings (matched against available contexts)
                           If neither specified, runs on all available contexts

Parallelism & Performance:
  --batch N                Cluster-level parallelism (default: 2)
                           Maintains constant parallelism: when one cluster finishes,
                           immediately starts the next one
  --ns-batch-size M        Number of namespaces in each namespace batch (default: 10)
  --ns-workers W           Thread pool size for oc checks per namespace batch (default: 5)

Namespace Filtering:
  --include-ns regex,...   Comma-separated regex patterns to include (namespace must match any)
                           Examples: --include-ns "tenant|prod"
  --exclude-ns regex,...   Comma-separated regex patterns to exclude (if match any -> excluded)
                           Examples: --exclude-ns "test|debug"
  --include-ephemeral      Include ephemeral test and cluster namespaces (default: excluded)
                           Ephemeral namespaces include:
                           - Ephemeral cluster namespaces: clusters-<uuid> pattern
                           - Ephemeral test namespaces: test-*, e2e-*, ephemeral-*, ci-*, etc.
                           On EaaS clusters, ephemeral namespaces are excluded by default
                           to avoid false positives from temporary test environments.

Time Range Filtering:
  --time-range RANGE       Time range to look back for events (default: 1d)
                           Format: <number><unit> where unit is:
                           s=seconds, m=minutes, h=hours, d=days, M=months (30 days)
                           Examples: 30s, 1h, 6h, 1d, 7d, 1M
  --plot-range RANGE       Time range for historical graph in HTML report (default: 2M).
                           Same format as --time-range. Used with/without --print-summary-from-dir.

Resilience & Timeouts:
  --retries R              Number of retries for oc calls (default: 3)
  --timeout S              OC request timeout in seconds used as --request-timeout (default: 45)

Output:
  --output DIR             Directory to save output files (default: output)
  All output formats are generated automatically:
  - oom_results.json       Structured JSON with metadata
  - oom_results.csv        Spreadsheet-friendly CSV format
  - oom_results.table      Human-readable table format
  - oom_results.html       Standalone HTML report (open in browser)
  At the end, a per-pod summary is printed (same format as
  oom_logs_and_desc_bundle_generator). CODEOWNERS owners are resolved by shallow-cloning
  konflux-release-data to a temp directory when -c is omitted, or by using -c DIR.
  Then, for each pod in the summary, date-wise tarballs are generated (same as
  running oom_logs_and_desc_bundle_generator -p <pod> -d <output> for each pod).
  Use --no-tarballs to skip tarball generation.

  -c, --codeowners-dir DIR  Path to an existing konflux-release-data
                            checkout. If omitted, the script shallow-clones
                            releng/konflux-release-data to a temp dir
                            (requires git + network/SSH). Per-pod summary
                            shows owner (name + email via glab).

  --no-tarballs            Do not generate per-pod tarballs after the
                           run (default: generate tarballs).

Debug & Troubleshooting:
  -v, --verbose            Show which namespaces are scanned or skipped (ephemeral/include/exclude)
  --list-namespaces        Print namespaces that would be scanned
                           (per context) and exit. Use to verify a
                           namespace (e.g. preflight-dev-tenant) is
                           included.

Testing (no cluster run):
  --print-summary-from-dir [DIR]  Print per-pod summary and generate
                                   oom_results.html from existing CSVs in
                                   DIR (default: output). No cluster run.
                                   Uses oom_results.csv as \"current run\"
                                   and oom_results_*_*.csv for historical
                                   graph.

Other:
  -h, --help               Show this help message

Examples:
  # Run on current context only
  ./oc_get_ooms.py --current

  # Run on specific contexts using substrings
  ./oc_get_ooms.py --contexts kflux-prd-rh02,stone-prd-rh01

  # Custom output directory (saves CSV/JSON/HTML/TABLE and artifacts under DIR)
  ./oc_get_ooms.py --output /path/to/reports
  ./oc_get_ooms.py --current --output my-oom-run

  # High-performance mode for large clusters
  ./oc_get_ooms.py --batch 4 --ns-batch-size 250 --ns-workers 250 --timeout 200

  # Filter by time range (last 6 hours)
  ./oc_get_ooms.py --time-range 6h

  # Include only tenant namespaces, exclude test namespaces
  ./oc_get_ooms.py --include-ns tenant --exclude-ns test

  # Combine multiple options
  ./oc_get_ooms.py --contexts prod-cluster --time-range 1d --include-ns "tenant|prod" --batch 4

  # Many options together: contexts, time range, ns filters, parallelism, output dir, codeowners
  ./oc_get_ooms.py --contexts prod,staging --time-range 7d --include-ns tenant --exclude-ns test \\
    --batch 4 --ns-batch-size 50 --ns-workers 20 --retries 5 --timeout 120 \\
    --output my-reports -c /path/to/konflux-release-data --verbose

  # All contexts, last 7 days, with custom parallelism
  ./oc_get_ooms.py --time-range 7d --batch 8 --ns-batch-size 100 --ns-workers 50

  # Regenerate HTML report from existing CSVs (no cluster run)
  ./oc_get_ooms.py --print-summary-from-dir output
  ./oc_get_ooms.py --print-summary-from-dir /path/to/output

  # Verify which namespaces will be scanned (e.g. check if preflight-dev-tenant is included)
  ./oc_get_ooms.py --contexts stone-stg-rh01 --list-namespaces | grep preflight

  # Verbose run to see skipped vs scanned namespaces
  ./oc_get_ooms.py --contexts stone-stg-rh01 --verbose --time-range 1d
"""
    )
    sys.exit(1)


def compile_patterns(csv_patterns: str | None) -> list[Pattern] | None:
    if not csv_patterns:
        return None
    parts = [p.strip() for p in csv_patterns.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [re.compile(p) for p in parts]
    except re.error as e:
        print(color(f"Invalid regex in patterns: {e}", RED))
        sys.exit(1)


def parse_args(
    argv: list[str],
) -> tuple[
    list[str],
    int,
    int,
    int,
    int,
    int | None,
    str,
    int,
    str,
    bool,
    bool,
    bool,
    str | None,
    str | None,
    str,
]:
    args = list(argv)
    if "--help" in args or "-h" in args:
        print_usage_and_exit()

    contexts: list[str] = []
    batch_size = DEFAULT_BATCH_SIZE
    ns_batch_size = DEFAULT_NS_BATCH_SIZE
    ns_workers = DEFAULT_NS_WORKERS
    retries = DEFAULT_RETRIES
    oc_timeout_seconds = DEFAULT_OC_TIMEOUT
    include_csv = None
    exclude_csv = None
    time_range_str = "1d"  # Default 1 day
    plot_range_str = "2M"  # Default 2 months for historical graph
    exclude_ephemeral = True  # Default: exclude ephemeral namespaces
    verbose = False
    list_namespaces = False
    codeowners_dir: str | None = None
    print_summary_from_dir: str | None = None
    output_dir_str = "output"
    no_tarballs = "--no-tarballs" in args

    if "--output" in args:
        i = args.index("--output")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --output", RED))
            print_usage_and_exit()
        output_dir_str = args[i + 1]

    if "--print-summary-from-dir" in args:
        i = args.index("--print-summary-from-dir")
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            print_summary_from_dir = args[i + 1].strip()
        else:
            print_summary_from_dir = output_dir_str

    if "--current" in args:
        cur = get_current_context(retries=retries, oc_timeout_seconds=oc_timeout_seconds)
        if cur:
            contexts = [cur]
    elif "--contexts" in args:
        i = args.index("--contexts")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --contexts", RED))
            print_usage_and_exit()
        context_substrings = [c.strip() for c in args[i + 1].split(",") if c.strip()]
        # Get all available contexts and match substrings
        available_contexts = get_all_contexts(
            retries=retries, oc_timeout_seconds=oc_timeout_seconds
        )
        if not available_contexts:
            print(
                color(
                    "ERROR: Could not retrieve available contexts. "
                    "Please check your oc/kubectl configuration.",
                    RED,
                )
            )
            sys.exit(1)
        contexts = match_contexts_by_substring(context_substrings, available_contexts)
    else:
        contexts = get_all_contexts(retries=retries, oc_timeout_seconds=oc_timeout_seconds)

    if "--batch" in args:
        i = args.index("--batch")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --batch", RED))
            print_usage_and_exit()
        try:
            batch_size = int(args[i + 1])
            if batch_size < 1:
                raise ValueError("batch size must be >= 1")
        except (ValueError, IndexError) as e:
            print(color(f"ERROR: invalid --batch value: {e}", RED))
            print_usage_and_exit()

    if "--ns-batch-size" in args:
        i = args.index("--ns-batch-size")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --ns-batch-size", RED))
            print_usage_and_exit()
        try:
            ns_batch_size = int(args[i + 1])
            if ns_batch_size < 1:
                raise ValueError("ns-batch-size must be >= 1")
        except (ValueError, IndexError) as e:
            print(color(f"ERROR: invalid --ns-batch-size value: {e}", RED))
            print_usage_and_exit()

    if "--ns-workers" in args:
        i = args.index("--ns-workers")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --ns-workers", RED))
            print_usage_and_exit()
        try:
            ns_workers = int(args[i + 1])
            if ns_workers < 1:
                raise ValueError("ns-workers must be >= 1")
        except (ValueError, IndexError) as e:
            print(color(f"ERROR: invalid --ns-workers value: {e}", RED))
            print_usage_and_exit()

    if "--include-ns" in args:
        i = args.index("--include-ns")
        include_csv = args[i + 1] if i + 1 < len(args) else None

    if "--exclude-ns" in args:
        i = args.index("--exclude-ns")
        exclude_csv = args[i + 1] if i + 1 < len(args) else None

    if "--include-ephemeral" in args:
        exclude_ephemeral = False  # User wants to include ephemeral namespaces

    if "--verbose" in args or "-v" in args:
        verbose = True

    if "--list-namespaces" in args:
        list_namespaces = True

    if "--retries" in args:
        i = args.index("--retries")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --retries", RED))
            print_usage_and_exit()
        try:
            retries = int(args[i + 1])
            if retries < 1:
                raise ValueError("retries must be >= 1")
        except (ValueError, IndexError) as e:
            print(color(f"ERROR: invalid --retries value: {e}", RED))
            print_usage_and_exit()

    if "--timeout" in args:
        i = args.index("--timeout")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --timeout", RED))
            print_usage_and_exit()
        try:
            oc_timeout_seconds = int(args[i + 1])
            if oc_timeout_seconds < 1:
                raise ValueError("timeout must be >= 1")
        except (ValueError, IndexError) as e:
            print(color(f"ERROR: invalid --timeout value: {e}", RED))
            print_usage_and_exit()

    if "--time-range" in args:
        i = args.index("--time-range")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --time-range", RED))
            print_usage_and_exit()
        time_range_str = args[i + 1]
        try:
            # Validate the format
            parse_time_range(time_range_str)
        except ValueError as e:
            print(color(f"ERROR: invalid --time-range value: {e}", RED))
            print_usage_and_exit()

    if "--plot-range" in args:
        i = args.index("--plot-range")
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --plot-range", RED))
            print_usage_and_exit()
        plot_range_str = args[i + 1]
        try:
            parse_time_range(plot_range_str)
        except ValueError as e:
            print(color(f"ERROR: invalid --plot-range value: {e}", RED))
            print_usage_and_exit()

    if "--codeowners-dir" in args or "-c" in args:
        flag = "--codeowners-dir" if "--codeowners-dir" in args else "-c"
        i = args.index(flag)
        if i + 1 >= len(args):
            print(color("ERROR: missing argument for --codeowners-dir", RED))
            print_usage_and_exit()
        codeowners_dir = args[i + 1].strip()
        if not codeowners_dir:
            codeowners_dir = None

    global _INCLUDE_PATTERNS, _EXCLUDE_PATTERNS, _VERBOSE, _LIST_NAMESPACES
    _INCLUDE_PATTERNS = compile_patterns(include_csv)
    _EXCLUDE_PATTERNS = compile_patterns(exclude_csv)
    _VERBOSE = verbose
    _LIST_NAMESPACES = list_namespaces

    # Parse time range to seconds
    try:
        time_range_seconds = parse_time_range(time_range_str)
    except ValueError:
        time_range_seconds = 86400  # Default to 1 day if parsing fails
    try:
        plot_range_seconds = parse_time_range(plot_range_str)
    except ValueError:
        plot_range_seconds = 5184000  # 2 months in seconds

    return (
        contexts,
        batch_size,
        ns_batch_size,
        ns_workers,
        retries,
        oc_timeout_seconds,
        time_range_seconds,
        time_range_str,
        plot_range_seconds,
        plot_range_str,
        exclude_ephemeral,
        verbose,
        list_namespaces,
        codeowners_dir,
        print_summary_from_dir,
        output_dir_str,
        no_tarballs,
    )


def main() -> None:
    """Main entry point for the OOM/CrashLoopBackOff detector."""
    # Configure logging (quiet by default, can be enhanced with --verbose flag)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    (
        contexts,
        batch_size,
        ns_batch_size,
        ns_workers,
        retries,
        oc_timeout_seconds,
        time_range_seconds,
        time_range_str,
        plot_range_seconds,
        plot_range_str,
        exclude_ephemeral,
        verbose,
        list_namespaces,
        codeowners_dir,
        print_summary_from_dir,
        output_dir_str,
        no_tarballs,
    ) = parse_args(sys.argv[1:])

    # --print-summary-from-dir: print summary and generate HTML from existing CSVs (no cluster run)
    if print_summary_from_dir is not None:
        # Resolve relative paths (e.g. "output") relative to the script's directory,
        # so the same dir is used regardless of current working directory.
        p = Path(print_summary_from_dir)
        if not p.is_absolute():
            script_dir = Path(__file__).resolve().parent
            out_dir = (script_dir / print_summary_from_dir).resolve()
        else:
            out_dir = p.resolve()
        main_csv = out_dir / "oom_results.csv"
        if not main_csv.is_file():
            print(
                color(
                    f"ERROR: {main_csv} not found. Run oc_get_ooms.py first to generate CSVs.", RED
                )
            )
            sys.exit(1)
        mtime = main_csv.stat().st_mtime
        run_date_str = datetime.fromtimestamp(mtime).strftime("%d-%b-%Y")
        current_run_rows = _read_csv_rows_with_date(main_csv, run_date_str)
        codeowners_path = resolve_codeowners_dir(codeowners_dir)
        print_per_pod_summary(
            current_run_rows,
            run_date_str,
            output_dir=out_dir,
            codeowners_dir=codeowners_path,
        )
        # Generate oom_results.html from existing data (graph + summary + detailed findings)
        if generate_html_report is not None:
            historical_series = build_historical_series_from_output_dir(out_dir, plot_range_seconds)
            historical_series_by_cluster = build_historical_series_by_cluster_from_output_dir(
                out_dir, plot_range_seconds
            )
            historical_html_links = get_historical_html_links(out_dir)
            if historical_series:
                print(
                    color(f"Historical graph: {len(historical_series)} run(s) in plot range.", BLUE)
                )
            else:
                print(
                    color(
                        "Historical graph: no timestamped runs in plot range"
                        " (oom_results_*_*.csv).",
                        YELLOW,
                    )
                )
            html_path = out_dir / "oom_results.html"
            try:
                generate_html_report(
                    rows=current_run_rows,
                    time_range_str=time_range_str,
                    html_path=html_path,
                    report_generated_est=report_generated_est(),
                    historical_series=historical_series,
                    historical_series_by_cluster=historical_series_by_cluster,
                    historical_html_links=historical_html_links,
                    plot_range_str=plot_range_str,
                )
                print(color(f"HTML report written → {html_path}", GREEN))
            except Exception as e:
                logging.warning(f"Failed to write HTML report: {e}")
                print(color(f"WARNING: Failed to write HTML report: {e}", YELLOW))
        sys.exit(0)

    if not contexts:
        print(color("No contexts discovered. Exiting.", RED))
        sys.exit(1)

    # --list-namespaces: print namespaces that would be scanned per context and exit
    if list_namespaces:
        for ctx in contexts:
            namespaces = get_namespaces_for_context(
                ctx,
                retries=retries,
                oc_timeout_seconds=oc_timeout_seconds,
                include_patterns=_INCLUDE_PATTERNS,
                exclude_patterns=_EXCLUDE_PATTERNS,
                exclude_ephemeral=exclude_ephemeral,
            )
            cluster = short_cluster_name(ctx)
            print(
                color(f"Context: {ctx} (cluster: {cluster}) — {len(namespaces)} namespaces", BLUE)
            )
            for ns in sorted(namespaces):
                print(ns)
        sys.exit(0)

    print(color(f"Using contexts: {contexts}", BLUE))
    print(
        color(
            f"Cluster-parallelism: {batch_size}  NS-batch-size: {ns_batch_size}  "
            f"NS-workers: {ns_workers}",
            BLUE,
        )
    )
    print(
        color(
            f"Retries: {retries}  OC timeout(s): {oc_timeout_seconds}s  "
            f"Time-range: {time_range_str}",
            BLUE,
        )
    )
    if exclude_ephemeral:
        print(
            color(
                "Ephemeral namespaces: EXCLUDED"
                " (ephemeral test/cluster namespaces will be skipped)",
                BLUE,
            )
        )
    else:
        print(
            color(
                "Ephemeral namespaces: INCLUDED (all namespaces will be scanned)",
                YELLOW,
            )
        )
    if _INCLUDE_PATTERNS:
        print(
            color(
                f"Include namespace patterns: {[p.pattern for p in _INCLUDE_PATTERNS]}",
                BLUE,
            )
        )
    if _EXCLUDE_PATTERNS:
        print(
            color(
                f"Exclude namespace patterns: {[p.pattern for p in _EXCLUDE_PATTERNS]}",
                BLUE,
            )
        )

    # Check cluster connectivity; proceed only if at least one cluster is connected
    _all_connected, connectivity_report = check_all_clusters_connectivity(
        contexts, retries=retries, oc_timeout_seconds=oc_timeout_seconds
    )
    print_connectivity_report_summary(connectivity_report)

    at_least_one_connected = any(connected for _, connected, _ in connectivity_report)
    if not at_least_one_connected:
        print(color("No clusters are accessible. Aborting.", RED))
        sys.exit(1)

    # Ensure output directory exists
    output_dir = ensure_output_directory(output_dir_str)

    # Move existing output files to output directory (one-time migration)
    move_existing_output_files(output_dir)

    results, skipped = run_batches(
        contexts,
        batch_size,
        retries,
        oc_timeout_seconds,
        ns_batch_size,
        ns_workers,
        time_range_seconds,
        exclude_ephemeral,
        output_dir=output_dir,
    )

    # All output files go to 'output' subdirectory
    json_path = output_dir / "oom_results.json"
    csv_path = output_dir / "oom_results.csv"
    table_path = output_dir / "oom_results.table"
    html_path = output_dir / "oom_results.html"

    # Backup existing files before generating new ones
    backup_output_files(json_path, csv_path, table_path, html_path)

    export_results(
        results,
        json_path,
        csv_path,
        table_path,
        html_path,
        time_range_str,
        output_dir=output_dir,
        plot_range_seconds=plot_range_seconds,
        plot_range_str=plot_range_str,
    )

    pretty_print(results, skipped)

    if skipped:
        print(
            color(
                "\nSome clusters were skipped due to connectivity errors (see messages above).",
                YELLOW,
            )
        )

    print(
        color(
            "\nPer-cluster logs written to"
            " output/logs_and_description_files/<cluster>/"
            " (if any findings were found)",
            GREEN,
        )
    )
    print(
        color(
            f"Output files written to '{output_dir}/' directory",
            GREEN,
        )
    )

    # Per-pod summary (base names, current run + historical from output dir)
    run_date_str = datetime.now().strftime("%d-%b-%Y")
    current_run_rows = collect_rows(results, "")
    for row in current_run_rows:
        row["date"] = run_date_str
    codeowners_path = resolve_codeowners_dir(codeowners_dir)
    print_per_pod_summary(
        current_run_rows,
        run_date_str,
        output_dir=output_dir,
        codeowners_dir=codeowners_path,
    )

    # Generate per-pod tarballs (same as oom_logs_and_desc_bundle_generator
    # -p <pod> -d <output> for each pod).
    # The bundle generator matches CSV pod column by substring; we must
    # pass a literal that appears in
    # actual pod names (not the display base name like "apiserver-*" which has asterisks).
    if not no_tarballs and current_run_rows:
        script_dir = Path(__file__).resolve().parent
        bundle_gen = script_dir / "oom_logs_and_desc_bundle_generator"
        # Group full pod names by display base_name, then compute a
        # match string (longest common prefix)
        base_to_pods: dict[str, list[str]] = defaultdict(list)
        for row in current_run_rows:
            base_to_pods[_pod_base_name(row["pod"])].append(row["pod"])
        if bundle_gen.is_file():
            print()
            print(color(f"Generating tarballs for {len(base_to_pods)} pod(s) ...", BLUE))
            for base_name in sorted(base_to_pods.keys()):
                pod_names = base_to_pods[base_name]
                match_str = _match_string_for_bundle_generator(pod_names)
                if not match_str:
                    continue
                cmd = [
                    "bash",
                    str(bundle_gen),
                    "-p",
                    match_str,
                    "-d",
                    str(output_dir),
                ]
                if codeowners_path is not None and codeowners_path.is_dir():
                    cmd.extend(["-c", str(codeowners_path)])
                rc = subprocess.run(cmd, cwd=str(script_dir))
                if rc.returncode != 0:
                    print(
                        color(
                            f"  Warning: tarball generation for pod"
                            f" '{base_name}' exited with {rc.returncode}",
                            YELLOW,
                        )
                    )
        else:
            print(color(f"  Skipping tarballs: {bundle_gen} not found", YELLOW))


if __name__ == "__main__":
    main()
