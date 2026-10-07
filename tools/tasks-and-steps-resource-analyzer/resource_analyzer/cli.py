"""CLI entrypoint for the tasks/steps resource analyzer."""

import argparse
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

from .clusters import (
    check_cluster_connectivity,
    prompt_confirmation,
    read_wrapper_config,
    validate_wrapper_steps,
)
from .paths import TOOL_DIR
from .prom import (
    POD_BATCH_SIZE,
    collect_individual_pod_executions,
    format_lookback_label,
    resolve_lookback_seconds,
)
from .reporting import (
    _clear_cluster_partials,
    check_comparison_file_exists_for_margin,
    check_files_exist_for_date,
    compute_cluster_coverage_report,
    find_latest_analysis_date,
    load_analyzed_data,
    load_comparison_data,
    save_analyzed_data,
    save_comparison_data_all_bases,
    save_detailed_per_step_data,
)
from .stats import (
    analyze_step_data_all_bases,
    detailed_executions_to_csv,
    parse_csv_data,
    print_comparison_table,
    verify_aggregates_against_detailed,
)
from .task_yaml import (
    compute_steps_missing_observability,
    extract_task_info,
    fetch_yaml_content,
    normalize_step_name_for_compare,
)


def main():
    _script_start = time.time()
    parser = argparse.ArgumentParser(
        description="Analyze resource consumption and provide recommendations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Phase 1: Analysis (default behavior):
    # From piped input:
    ./wrapper_for_promql_for_all_clusters.sh 7 --csv | ./analyze_resource_limits.py

    # From YAML file (auto-runs data collection):
    # Note: --base is IGNORED in Phase 1. All base metrics (max, p95, p90, median) are generated.
    ./analyze_resource_limits.py --file /path/to/buildah.yaml
    ./analyze_resource_limits.py --file https://github.com/.../buildah.yaml

    # Custom safety margin (default: 5%):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --margin 5
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --margin 10
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --margin 20

    # Custom data collection period (default: 7 days):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --days 1
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --days 10
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --days 30

    # Sub-day / combined lookback (--days and --hours are clubbed):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --days 0 --hours 6
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --days 1 --hours 6

    # Combine options (--base is ignored in Phase 1):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --margin 15 --days 10

  Parallel Processing (Phase 1 only):
    # Process multiple clusters concurrently:
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --pll-clusters 3
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --pll-clusters 5

    # Combine with other options:
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --pll-clusters 3 --days 10 --margin 5

  Phase 2: Update (view recommendations):
    # View recommendations for all base metrics (requires --file):
    # Note: --base flag is ignored. All base metrics (max, p95, p90, median) are always shown.
    # YAML files are NOT updated automatically.
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 5

    # Create comparison files for different margins (each margin gets its own file):
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 5
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 10
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 20
    # All three margin files coexist, allowing easy comparison

  Debug and Validation:
    # Enable debug output:
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --debug
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --debug

    # Dry-run: Validate task/steps and check cluster connectivity (Phase 1 only):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --dry-run
    ./analyze_resource_limits.py --file /path/to/buildah.yaml --dry-run --debug

  Complete Examples:
    # Phase 1: Full-featured analysis (generates all base metrics):
    ./analyze_resource_limits.py --file /path/to/buildah.yaml \\
        --margin 15 --days 10 --pll-clusters 4 --debug

    # Two-phase workflow (recommended):
    # Phase 1: Run analysis (generates all base metrics with margin 5%)
    ./analyze_resource_limits.py --file https://github.com/.../buildah.yaml --margin 5 --days 7
    # Phase 2: View recommendations for all base metrics with margin 5%
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 5
    # Phase 2: Create comparison files for margin 10% (separate file, coexists with margin 5% file)
    ./analyze_resource_limits.py --update --file /path/to/buildah.yaml --margin 10
        """,
    )
    parser.add_argument(
        "--file",
        help="YAML file path or GitHub URL to analyze (auto-runs data collection)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Phase 2: View recommendations for all base metrics"
            " with specified --margin. Requires --file to specify"
            " which task to update. Creates comparison files for"
            " each margin (if they don't exist). Does not modify"
            " YAML files (user must update manually)."
            " --base flag is ignored."
        ),
    )
    parser.add_argument(
        "--analyze-again",
        "--aa",
        dest="analyze_again",
        action="store_true",
        help="Force re-analysis even when cache exists (only used with --update --file)",
    )
    parser.add_argument(
        "--margin", type=int, default=5, help="Safety margin percentage (default: 5)"
    )
    parser.add_argument(
        "--base",
        type=str,
        choices=["max", "p95", "p90", "median"],
        default="max",
        help=(
            "Base metric for margin calculation: max, p95, p90,"
            " or median (default: max). Ignored in both Phase 1"
            " (analysis) and Phase 2 (--update). All base metrics"
            " are always generated and shown."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help=(
            "Whole days in the lookback window (default: 7). "
            "Combined with --hours (total = days*24h + hours). Use --days 0 with --hours for "
            "sub-day windows."
        ),
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=0,
        help=(
            "Additional hours in the lookback window (default: 0). "
            "Clubbed with --days, e.g. --days 1 --hours 6 => 30h total."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=("Enable debug output including pod counts and capped PromQL skip-reason samples"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate task/steps and check cluster connectivity without running data collection",
    )
    parser.add_argument(
        "--pll-clusters",
        type=int,
        metavar="N",
        help=(
            "Enable parallel processing across clusters with"
            " N workers (only during analysis, ignored during"
            " --update)"
        ),
    )
    parser.add_argument(
        "--pll-queries",
        type=int,
        metavar="N",
        default=2,
        help="Number of Prometheus queries to run in parallel per pod-batch "
        "(mem/cpu/io_read/io_write). Default: 2. Max effective value: 4 (one per metric). "
        "Higher values reduce wall-clock time at the cost of more concurrent HTTP requests.",
    )
    parser.add_argument(
        "--pll-pods",
        type=int,
        metavar="N",
        default=8,
        help="Number of pod-batch jobs to process in parallel per cluster worker "
        f"(each batch is up to {POD_BATCH_SIZE} pods for one step/namespace). "
        "Default: 8. Higher values reduce wall-clock time on large clusters.",
    )

    args = parser.parse_args()

    try:
        lookback_seconds = resolve_lookback_seconds(args.days, args.hours)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    lookback_label = format_lookback_label(args.days, args.hours)
    lookback_days_fraction = lookback_seconds / 86400.0

    # Phase 2: --update (requires --file)
    if args.update:
        if not args.file:
            print(
                "Error: --update requires --file to specify which task to update",
                file=sys.stderr,
            )
            sys.exit(1)

        # Load YAML to get task name and current resources
        yaml_content, yaml_path = fetch_yaml_content(args.file)
        task_name, file_steps, _, current_resources = extract_task_info(yaml_content)

        if not task_name:
            print("Error: Could not extract task name from YAML file", file=sys.stderr)
            sys.exit(1)

        # Find latest analysis date for this task
        analysis_date = find_latest_analysis_date(task_name)
        if not analysis_date:
            print(
                f"Error: No analysis data found for task '{task_name}'. Please run Phase 1"
                f" (analysis) first.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Load analyzed data
        analyzed_data = load_analyzed_data(task_name, analysis_date)
        if not analyzed_data:
            print(
                f"Error: Could not load analyzed data for task '{task_name}' (date:"
                f" {analysis_date})",
                file=sys.stderr,
            )
            sys.exit(1)

        # Check if comparison file exists for this margin
        comparison_file_exists = check_comparison_file_exists_for_margin(
            task_name, analysis_date, args.margin
        )

        if not comparison_file_exists:
            print(
                f"Comparison file for margin {args.margin}% does not exist. Creating new"
                f" comparison files...",
                file=sys.stderr,
            )

            # Need to generate recommendations for all base metrics with this margin
            # Load CSV data from analyzed_data
            csv_data = analyzed_data.get("csv_data")
            if not csv_data:
                print("Error: CSV data not found in analyzed data", file=sys.stderr)
                sys.exit(1)

            # Parse CSV and regenerate recommendations for all base metrics
            data = parse_csv_data(csv_data)
            if not data:
                print("Error: Could not parse CSV data", file=sys.stderr)
                sys.exit(1)

            # Group by step
            by_step = defaultdict(list)
            for row in data:
                step = row.get("step", "").strip()
                if step:
                    by_step[step].append(row)

            # Analyze each step for all base metrics
            all_recommendations_by_base = {
                "max": [],
                "p95": [],
                "p90": [],
                "median": [],
            }
            for step_name in sorted(by_step.keys()):
                step_all_bases = analyze_step_data_all_bases(
                    step_name, by_step[step_name], args.margin
                )
                if step_all_bases:
                    for base in ["max", "p95", "p90", "median"]:
                        all_recommendations_by_base[base].append(step_all_bases[base])

            steps_missing_obs = analyzed_data.get("steps_without_observability_data")
            if steps_missing_obs is None:
                steps_missing_obs = compute_steps_missing_observability(file_steps, by_step)
            # Save comparison data with new margin (no timestamp since no re-analysis)
            html_path, json_path = save_comparison_data_all_bases(
                task_name,
                all_recommendations_by_base,
                current_resources,
                args.margin,
                analysis_date,
                use_timestamp=False,
                steps_without_observability_data=steps_missing_obs,
            )
            print(f"Created comparison files for margin {args.margin}%:", file=sys.stderr)
            print(f"  - {html_path}", file=sys.stderr)
            print(f"  - {json_path}", file=sys.stderr)
        else:
            print(
                f"Comparison file for margin {args.margin}% already exists. Using existing file.",
                file=sys.stderr,
            )
            # Load existing comparison data
            comparison_data = load_comparison_data(task_name, analysis_date, args.margin)
            if not comparison_data:
                print("Error: Could not load comparison data", file=sys.stderr)
                sys.exit(1)
            all_recommendations_by_base = comparison_data.get("recommendations_by_base", {})

        # Show comparison tables for all base metrics (--base is ignored in Phase 2)
        print(
            f"\nShowing recommendations for all base metrics (margin: {args.margin}%):",
            file=sys.stderr,
        )
        print("Note: --base flag is ignored. All base metrics are shown.", file=sys.stderr)
        print()

        # Show comparison table for each base metric
        for base in ["max", "p95", "p90", "median"]:
            recommendations = all_recommendations_by_base.get(base, [])
            if recommendations:
                print(f"\n{'=' * 100}", file=sys.stderr)
                print(f"Base Metric: {base.upper()}", file=sys.stderr)
                print(f"{'=' * 100}", file=sys.stderr)
                print_comparison_table(
                    recommendations, current_resources, task_name, save_html=False
                )

        print(
            "\nNote: YAML file is NOT updated automatically. Please review the recommendations"
            "above",
            file=sys.stderr,
        )
        print("      and update the YAML file manually if needed.", file=sys.stderr)

        return

    # Phase 1: ANALYSIS (default, unless --update is specified)
    # --base is ignored in Phase 1, all base metrics are generated
    # Determine input source
    current_resources = None
    file_path_or_url = args.file

    if args.file:
        # Load YAML and extract task info
        yaml_content, yaml_path = fetch_yaml_content(args.file)
        yaml_task_name, yaml_steps, default_resources, current_resources = extract_task_info(
            yaml_content
        )

        if not yaml_task_name:
            print("Error: Could not extract task name from YAML", file=sys.stderr)
            sys.exit(1)

        if not yaml_steps:
            print("Error: Could not extract steps from YAML", file=sys.stderr)
            sys.exit(1)

        # Read wrapper script configuration
        script_dir = TOOL_DIR
        wrapper_path = script_dir / "wrapper_for_promql_for_all_clusters.sh"
        wrapper_task, wrapper_steps, wrapper_defined = read_wrapper_config(wrapper_path)

        # Determine which task/steps to use
        final_task_name = yaml_task_name
        final_steps = yaml_steps
        source_desc = "extracted from YAML file"

        if wrapper_defined:
            # Validate wrapper-defined values against YAML
            print("\n" + "=" * 80, file=sys.stderr)
            print("VALIDATION: Wrapper-defined Task and Steps", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            print(f"Wrapper Task: {wrapper_task}", file=sys.stderr)
            print(f"Wrapper Steps: {', '.join(wrapper_steps)}", file=sys.stderr)
            print(f"YAML Task: {yaml_task_name}", file=sys.stderr)
            print(f"YAML Steps: {', '.join(yaml_steps)}", file=sys.stderr)
            print("=" * 80, file=sys.stderr)

            is_valid, errors = validate_wrapper_steps(
                wrapper_task, wrapper_steps, yaml_task_name, yaml_steps
            )

            if not is_valid:
                print("\nERROR: Validation failed:", file=sys.stderr)
                for error in errors:
                    print(f"  - {error}", file=sys.stderr)
                print(
                    "\nPlease fix the wrapper script or use a different YAML file.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Show which steps will be used vs available
            wrapper_steps_set = set(wrapper_steps)
            yaml_steps_set = set(yaml_steps)
            missing_steps = yaml_steps_set - wrapper_steps_set

            if missing_steps:
                print(
                    f"\nINFO: YAML file has additional steps not in wrapper:"
                    f" {sorted(missing_steps)}",
                    file=sys.stderr,
                )
                print("  These steps will not be analyzed.", file=sys.stderr)

            print(
                "\n✓ Validation passed: Wrapper steps are valid subset of YAML steps",
                file=sys.stderr,
            )

            # Use wrapper's values
            final_task_name = wrapper_task
            final_steps = wrapper_steps
            source_desc = "defined in wrapper script (validated against YAML)"
        else:
            # Extract from YAML and prefix steps with 'step-'
            final_steps = [f"step-{s}" if not s.startswith("step-") else s for s in yaml_steps]
            source_desc = "extracted from YAML file"

        # Always check cluster connectivity (before proceeding with analysis)
        print("\n" + "=" * 80, file=sys.stderr)
        if args.dry_run:
            print("DRY-RUN: Checking Cluster Connectivity", file=sys.stderr)
        else:
            print("Checking Cluster Connectivity", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        all_connected, connectivity_report = check_cluster_connectivity(wrapper_path)

        print("\nCluster Connectivity Report:", file=sys.stderr)
        for cluster, connected, message in connectivity_report:
            status = "✓" if connected else "✗"
            print(f"  {status} {cluster}: {message}", file=sys.stderr)

        accessible_count = sum(1 for _, connected, _ in connectivity_report if connected)
        total_count = len(connectivity_report)

        cluster_summary = f"{accessible_count}/{total_count} clusters are accessible"
        if not all_connected:
            print(f"\nWARNING: {cluster_summary}.", file=sys.stderr)
            print(
                "  Data collection may fail for unreachable clusters.",
                file=sys.stderr,
            )
            if not args.dry_run:
                print(
                    "  Continuing with accessible clusters only...",
                    file=sys.stderr,
                )
        else:
            print(f"\n✓ {cluster_summary}", file=sys.stderr)

        # For display, ensure steps have 'step-' prefix (for consistency with wrapper script format)
        steps_for_display = [f"step-{s}" if not s.startswith("step-") else s for s in final_steps]
        if not prompt_confirmation(final_task_name, steps_for_display, source_desc):
            print("Aborted by user.", file=sys.stderr)
            sys.exit(0)

        # If dry-run, exit here
        if args.dry_run:
            print("\n" + "=" * 80, file=sys.stderr)
            print(
                "DRY-RUN completed successfully. No data collection performed.",
                file=sys.stderr,
            )
            print("=" * 80, file=sys.stderr)
            sys.exit(0)

        # --analyze-again: wipe partial checkpoints so we start a completely fresh collection
        if args.analyze_again:
            _clear_cluster_partials(final_task_name)

        # Convert final_steps to list without leading 'step-' prefix for collection (step names in
        # reports stay
        # without prefix). Use prefix-only strip — do not use str.replace('step-',''), names like
        # step-fips-operator-check-step-action contain 'step-' inside the suffix and would be
        # corrupted.
        steps_for_collection = [normalize_step_name_for_compare(s) for s in final_steps]

        # Single source of truth: collect detailed per-pod data, then derive CSV for analysis
        parallel_workers = args.pll_clusters if args.pll_clusters and not args.update else None
        pll_queries = max(1, min(4, args.pll_queries)) if args.pll_queries else 2
        pll_pods = max(1, args.pll_pods) if args.pll_pods else 8
        detailed_executions, collection_stats = collect_individual_pod_executions(
            final_task_name,
            steps_for_collection,
            days=args.days,
            hours=args.hours,
            lookback_seconds=lookback_seconds,
            parallel_clusters=parallel_workers,
            debug=args.debug,
            current_resources=current_resources,
            pll_queries=pll_queries,
            pll_pods=pll_pods,
        )

        # Finding 2: compute and print cluster data coverage report
        cluster_coverage_report = compute_cluster_coverage_report(
            detailed_executions, lookback_days_fraction
        )
        if cluster_coverage_report:
            print("\n" + "=" * 80, file=sys.stderr)
            print(
                "Cluster Data Coverage Report (oldest data point per cluster):",
                file=sys.stderr,
            )
            print("=" * 80, file=sys.stderr)
            any_short = False
            for cl in sorted(cluster_coverage_report):
                info = cluster_coverage_report[cl]
                ok = info["meets_requested"]
                mark = "✓" if ok else "⚠"
                print(
                    f"  {mark} {cl}: {info['days_covered']:.1f} days covered "
                    f"(oldest: {info['oldest_date']}, pods: {info['pod_count']})",
                    file=sys.stderr,
                )
                if not ok:
                    any_short = True
            if any_short:
                print(
                    f"\n  WARNING: Some clusters returned fewer than {lookback_label} of data.",
                    file=sys.stderr,
                )
                print(
                    "  Statistics may under-represent rare heavy workloads on those clusters.",
                    file=sys.stderr,
                )
            print("=" * 80, file=sys.stderr)

        csv_data = detailed_executions_to_csv(detailed_executions)
        if not csv_data or not csv_data.strip() or csv_data.count("\n") < 1:
            listed = collection_stats.get("pods_listed", 0)
            queried = collection_stats.get("pods_queried", 0)
            kept = collection_stats.get("pods_kept", 0)
            qfail = collection_stats.get("query_failures", 0)
            empty = collection_stats.get("empty_metrics", 0)
            parse_err = collection_stats.get("parse_errors", 0)
            list_fail = collection_stats.get("list_failures", 0)
            if listed == 0:
                print(
                    "Error: No data from detailed collection — 0 pods listed across all clusters.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Error: No data from detailed collection — "
                    f"{listed} pods listed, {queried} pod×step queries, "
                    f"{kept} kept "
                    f"(query_failures={qfail}, empty_metrics={empty}, "
                    f"parse_errors={parse_err}, list_failures={list_fail}).",
                    file=sys.stderr,
                )
            if steps_for_collection:
                print(f"\nTask: {final_task_name}", file=sys.stderr)
                print(f"Steps: {', '.join(steps_for_collection)}", file=sys.stderr)
                print(f"Lookback: {lookback_label}", file=sys.stderr)
            if args.debug and collection_stats.get("debug_samples"):
                print(
                    "\nRe-run tip: inspect DEBUG skip samples above for root cause.",
                    file=sys.stderr,
                )
            sys.exit(1)
        # Show table (same as wrapper path)
        format_script = TOOL_DIR / "format_csv_table.py"
        if format_script.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(format_script)],
                    input=csv_data,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout:
                    print(result.stdout)
            except (subprocess.TimeoutExpired, Exception):
                pass
        task_name = final_task_name
        steps = steps_for_collection
    else:
        # Read from stdin (e.g. user ran wrapper and piped CSV)
        detailed_executions = None
        cluster_coverage_report = {}
        if sys.stdin.isatty():
            print(
                "Error: No input provided. Use --file or pipe CSV data.",
                file=sys.stderr,
            )
            sys.exit(1)
        csv_data = sys.stdin.read()
        yaml_content = None
        task_name = None
        steps = None
        file_path_or_url = None

    # Parse CSV data
    data = parse_csv_data(csv_data)
    if not data:
        print("Error: No data found in CSV input", file=sys.stderr)
        print("\nPossible reasons:", file=sys.stderr)
        print(
            "  1. No pods found for the specified task/steps in the given time period",
            file=sys.stderr,
        )
        print(
            "  2. Task or step names don't match what's actually running in clusters",
            file=sys.stderr,
        )
        print("  3. Cluster connectivity issues (try --dry-run to check)", file=sys.stderr)
        print(
            "  4. Time period too short (try increasing --days / --hours)",
            file=sys.stderr,
        )
        if args.file:
            print(f"\nTask: {task_name}", file=sys.stderr)
            print(f"Steps: {', '.join(steps)}", file=sys.stderr)
            print(f"Lookback: {lookback_label}", file=sys.stderr)
        # Show first few lines of CSV to help debug
        if csv_data:
            csv_lines = csv_data.strip().split("\n")
            print(f"\nCSV output (first {min(5, len(csv_lines))} lines):", file=sys.stderr)
            for i, line in enumerate(csv_lines[:5], 1):
                print(f"  {i}: {line[:100]}", file=sys.stderr)
        sys.exit(1)

    # Group by step
    by_step = defaultdict(list)
    for row in data:
        step = row.get("step", "").strip()
        if step:
            by_step[step].append(row)

    steps_missing_obs = []
    if steps:
        steps_missing_obs = compute_steps_missing_observability(steps, by_step)
        if steps_missing_obs:
            print("", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            print(
                "WARNING: YAML step(s) with no observability data in this run",
                file=sys.stderr,
            )
            print("=" * 80, file=sys.stderr)
            for s in steps_missing_obs:
                print(f"  - {s}", file=sys.stderr)
            print("", file=sys.stderr)
            print(
                "Recommendations and aggregate tables only include steps with at least one",
                file=sys.stderr,
            )
            print(
                'Prometheus sample for container="step-<name>" in the analysis window.',
                file=sys.stderr,
            )
            print(
                "Common causes: TaskRuns using older bundles without that step; git-resolved",
                file=sys.stderr,
            )
            print(
                "StepAction with a different runtime container name; or no pods in --days.",
                file=sys.stderr,
            )
            print("=" * 80, file=sys.stderr)

    # Phase 1: Analyze each step for ALL base metrics (ignore --base flag)
    # Note: --base is ignored in Phase 1, all metrics (max, p95, p90, median) are generated
    all_recommendations_by_base = {"max": [], "p95": [], "p90": [], "median": []}
    for step_name in sorted(by_step.keys()):
        step_all_bases = analyze_step_data_all_bases(step_name, by_step[step_name], args.margin)
        if step_all_bases:
            for base in ["max", "p95", "p90", "median"]:
                all_recommendations_by_base[base].append(step_all_bases[base])

    # Get date string for file naming (YYYYMMDD format)
    date_str = datetime.now().strftime("%Y%m%d")

    # Check if analyzed_data files exist for this date (indicates re-analysis)
    # This check happens BEFORE saving, so we know if we need timestamp
    analyzed_files_exist = (
        check_files_exist_for_date(task_name, "analyzed_data", date_str)
        if file_path_or_url and task_name
        else False
    )

    # Save analyzed_data (CSV data) - use timestamp if re-analysis happened
    analyzed_html_path = None
    analyzed_json_path = None
    if file_path_or_url and task_name and csv_data:
        analyzed_html_path, analyzed_json_path = save_analyzed_data(
            task_name,
            csv_data,
            date_str,
            steps_without_observability_data=steps_missing_obs,
            cluster_coverage_report=cluster_coverage_report if args.file else None,
            days_requested=lookback_days_fraction if args.file else None,
        )
        print("\nSaved analyzed data:", file=sys.stderr)
        print(f"  - {analyzed_html_path}", file=sys.stderr)
        print(f"  - {analyzed_json_path}", file=sys.stderr)

    # Save comparison_data (all base metrics) - use timestamp if re-analysis happened
    comparison_html_path = None
    comparison_json_path = None
    if file_path_or_url and task_name:
        comparison_html_path, comparison_json_path = save_comparison_data_all_bases(
            task_name,
            all_recommendations_by_base,
            current_resources,
            args.margin,
            date_str,
            use_timestamp=analyzed_files_exist,
            steps_without_observability_data=steps_missing_obs,
            cluster_coverage_report=cluster_coverage_report if args.file else None,
            days_requested=lookback_days_fraction if args.file else None,
            detailed_executions=detailed_executions if detailed_executions else None,
        )
        print(
            f"\nSaved comparison data (all base metrics, margin={args.margin}%):",
            file=sys.stderr,
        )
        print(f"  - {comparison_html_path}", file=sys.stderr)
        print(f"  - {comparison_json_path}", file=sys.stderr)

    # Save detailed per-step data (one HTML/JSON/CSV per step); use already-collected executions
    # when from --file (single source)
    if file_path_or_url and task_name and steps and detailed_executions:
        detailed_paths = save_detailed_per_step_data(task_name, detailed_executions, date_str)
        print("\nSaved detailed per-step data (one file per step):", file=sys.stderr)
        for html_path, json_path, csv_path in detailed_paths:
            print(f"  - {html_path}", file=sys.stderr)
            print(f"  - {json_path}", file=sys.stderr)
            print(f"  - {csv_path}", file=sys.stderr)

    # In-memory verification: aggregated main table vs recomputation from detailed executions (only
    # when we have both)
    if detailed_executions and data:
        verify_ok, verify_messages = verify_aggregates_against_detailed(detailed_executions, data)
        if verify_messages:
            print("\n" + "=" * 80, file=sys.stderr)
            print("Aggregate verification (main table vs detailed data):", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            for msg in verify_messages:
                print(f"  {msg}", file=sys.stderr)
        if verify_ok:
            print(
                "\n✓ Aggregate verification passed: main table matches recomputation from"
                "detailed executions.",
                file=sys.stderr,
            )
        else:
            print(
                "\n✗ Aggregate verification found mismatches (see above). Please report if this"
                "persists.",
                file=sys.stderr,
            )

    print(
        "\nPhase 1 (Analysis) completed. All base metrics (max, p95, p90, median) have been"
        "generated.",
        file=sys.stderr,
    )
    if steps_missing_obs:
        print(
            f"Note: {len(steps_missing_obs)} YAML step(s) had no observability data (see"
            f" WARNING above; "
            f"also steps_without_observability_data in saved JSON).",
            file=sys.stderr,
        )
    if comparison_html_path:
        print(
            "\nReview the comparison report to choose your base metric (max / p95 / p90 / median):",
            file=sys.stderr,
        )
        print(f"  → {comparison_html_path}", file=sys.stderr)
    print(
        "\nRun Phase 2 (--update) with --file to apply recommendations for a specific base metric.",
        file=sys.stderr,
    )

    elapsed = time.time() - _script_start
    mins, secs = divmod(int(elapsed), 60)
    print(f"\nTotal time: {mins}m {secs}s", file=sys.stderr)
