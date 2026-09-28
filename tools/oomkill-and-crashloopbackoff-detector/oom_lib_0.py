from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oom_constants import (
    _CLI_TOOL,
    BLUE,
    DEFAULT_RETRIES,
    GREEN,
    RED,
    RESET,
    RETRY_DELAY_SECONDS,
    YELLOW,
)


def color(text: str, c: str) -> str:
    return f"{c}{text}{RESET}"


def run_cmd_with_retries(
    cmd: list[str], retries: int = DEFAULT_RETRIES, timeout: int | None = None
) -> tuple[int, str, str]:
    attempt = 0
    last_err = ""
    while attempt < max(1, retries):
        attempt += 1
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            stdout = (completed.stdout or "").strip()
            stderr = (completed.stderr or "").strip()
            return completed.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            last_err = f"TimeoutExpired after {timeout}s"
            time.sleep(RETRY_DELAY_SECONDS * attempt)
        except Exception as e:
            last_err = str(e)
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    return 1, "", last_err


def run_shell_cmd_with_retries(
    cmd: str, retries: int = DEFAULT_RETRIES, timeout: int | None = None
) -> tuple[int, str, str]:
    return run_cmd_with_retries(["/bin/sh", "-c", cmd], retries=retries, timeout=timeout)


def detect_cli_tool() -> str:
    """
    Detect which CLI tool to use: kubectl (preferred) or oc (fallback).

    Returns:
        "kubectl" if available, "oc" if kubectl not available, or raises error if neither found
    """
    global _CLI_TOOL
    if _CLI_TOOL:
        return _CLI_TOOL

    # Try kubectl first (works with any Kubernetes cluster)
    rc, _, _ = run_cmd_with_retries(
        ["kubectl", "version", "--client", "--short"], retries=1, timeout=5
    )
    if rc == 0:
        _CLI_TOOL = "kubectl"
        return _CLI_TOOL

    # Fallback to oc (OpenShift)
    rc, _, _ = run_cmd_with_retries(["oc", "version", "--client"], retries=1, timeout=5)
    if rc == 0:
        _CLI_TOOL = "oc"
        return _CLI_TOOL

    # Neither found
    raise RuntimeError(
        "Neither 'kubectl' nor 'oc' CLI tool found. "
        "Please install kubectl (for Kubernetes) or oc (for OpenShift)."
    )


def cli_cmd_parts(context: str, cli_timeout_seconds: int, subcommand: list[str]) -> list[str]:
    """Build command parts for kubectl or oc."""
    cli_tool = detect_cli_tool()
    parts = [cli_tool, f"--request-timeout={cli_timeout_seconds}s"]
    if context:
        parts += ["--context", context]
    parts += subcommand
    return parts


def run_cli_subcommand(
    context: str, subcommand: list[str], retries: int, cli_timeout_seconds: int
) -> tuple[int, str, str]:
    """Run a kubectl or oc subcommand."""
    cmd = cli_cmd_parts(context, cli_timeout_seconds, subcommand)
    return run_cmd_with_retries(cmd, retries=retries, timeout=cli_timeout_seconds + 5)


def oc_cmd_parts(context: str, oc_timeout_seconds: int, subcommand: list[str]) -> list[str]:
    """Backward compatibility alias for cli_cmd_parts."""
    return cli_cmd_parts(context, oc_timeout_seconds, subcommand)


def run_oc_subcommand(
    context: str, subcommand: list[str], retries: int, oc_timeout_seconds: int
) -> tuple[int, str, str]:
    """Backward compatibility alias for run_cli_subcommand."""
    return run_cli_subcommand(context, subcommand, retries, oc_timeout_seconds)


def get_all_contexts(retries: int, oc_timeout_seconds: int) -> list[str]:
    """Get all available Kubernetes/OpenShift contexts."""
    cli_tool = detect_cli_tool()
    cmd = [cli_tool, "config", "get-contexts", "-o", "name"]
    rc, out, err = run_cmd_with_retries(cmd, retries=retries, timeout=oc_timeout_seconds + 5)
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def match_contexts_by_substring(
    substrings: list[str],
    available_contexts: list[str],
) -> list[str]:
    """
    Match context substrings against available contexts.

    Args:
        substrings: List of substrings to match (e.g., ['kflux-prd-rh02'])
        available_contexts: List of all available context names

    Returns:
        List of matched full context names

    Raises:
        SystemExit: If no match or multiple matches found for a substring
    """
    matched_contexts = []
    for substring in substrings:
        matches = [ctx for ctx in available_contexts if substring.lower() in ctx.lower()]
        if not matches:
            print(
                color(
                    f"ERROR: No context found matching substring '{substring}'",
                    RED,
                )
            )
            print(color("Available contexts:", YELLOW))
            for ctx in available_contexts:
                print(f"  - {ctx}")
            sys.exit(1)
        elif len(matches) > 1:
            print(
                color(
                    f"ERROR: Multiple contexts match substring '{substring}':",
                    RED,
                )
            )
            for ctx in matches:
                print(f"  - {ctx}")
            print(
                color(
                    "Please use a more specific substring to uniquely identify the context.",
                    YELLOW,
                )
            )
            sys.exit(1)
        else:
            matched_contexts.append(matches[0])
            print(
                color(
                    f"Matched '{substring}' -> '{matches[0]}'",
                    GREEN,
                )
            )
    return matched_contexts


def get_current_context(retries: int, oc_timeout_seconds: int) -> str:
    """Get the current Kubernetes/OpenShift context."""
    cli_tool = detect_cli_tool()
    cmd = [cli_tool, "config", "current-context"]
    rc, out, err = run_cmd_with_retries(cmd, retries=retries, timeout=oc_timeout_seconds + 5)
    return out.strip() if rc == 0 else ""


def short_cluster_name(full_ctx: str) -> str:
    m = re.search(r"api-([^-]+-[^-]+-[^-]+)", full_ctx)
    if m:
        return m.group(1)
    if "/" in full_ctx:
        return full_ctx.split("/")[-1]
    return full_ctx.replace("/", "_").replace(":", "_")


def parse_timestamp_to_iso(ts: str) -> str:
    """Parse Kubernetes timestamp to ISO format."""
    if not ts:
        return ""
    try:
        base = ts.split(".")[0].rstrip("Z")
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError) as e:
        logging.debug(f"Failed to parse timestamp '{ts}': {e}")
        return ts


def _parse_kubernetes_timestamp_utc(ts: str) -> float | None:
    """
    Parse a Kubernetes timestamp string (RFC3339, typically UTC with Z) to Unix seconds.
    Returns None if ts is empty or unparseable.
    """
    if not ts or not ts.strip():
        return None
    try:
        base = ts.split(".")[0].rstrip("Z")
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=UTC).timestamp()
    except (ValueError, AttributeError):
        return None


def _timestamp_in_range(ts_str: str, cutoff_time: float) -> bool:
    """
    Return True if the finding should be included for time-range filtering.
    - If ts_str is empty: include (we don't drop findings with no timestamp).
    - Otherwise: include only if parsed timestamp (as UTC) >= cutoff_time.
    """
    if not ts_str or not ts_str.strip():
        return True
    parsed = _parse_kubernetes_timestamp_utc(ts_str)
    if parsed is None:
        return True
    return parsed >= cutoff_time


def now_ts_for_filename() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def timestamp_for_backup() -> str:
    """Generate a readable timestamp string for backup filenames.

    Returns format like: '12-Jan-2026_12-05-57-EST'
    """
    now = datetime.now()
    # Get timezone abbreviation (EST, PST, etc.)
    tz_abbr = "UTC"
    try:
        # Try strftime first
        tz_str = now.strftime("%Z")
        if tz_str and tz_str.strip():
            tz_abbr = tz_str
        else:
            # Fallback: use time.tzname
            import time

            if time.tzname and len(time.tzname) > 0:
                tz_abbr = time.tzname[0] if time.daylight == 0 else time.tzname[1]
    except Exception:
        # If all else fails, use UTC
        tz_abbr = "UTC"

    # Format: DD-MMM-YYYY_HH-MM-SS-TZ
    return now.strftime(f"%d-%b-%Y_%H-%M-%S-{tz_abbr}")


def report_generated_est() -> str:
    """Return current time formatted for report header, preferably in EST (America/New_York)."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
        return now.strftime("%d-%b-%Y %H:%M:%S %Z")
    except Exception:
        now = datetime.now(UTC)
        return now.strftime("%d-%b-%Y %H:%M:%S UTC")


def timestamp_for_backup_from_file(file_path: Path) -> str:
    """Generate a timestamp string for backup filenames using the file's last modified time.

    Same format as timestamp_for_backup(): e.g. '02-Feb-2026_10-38-49-EDT'.
    This keeps backup names aligned with the file's actual modification date.
    """
    mtime = file_path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime)
    tz_abbr = "UTC"
    try:
        tz_str = dt.strftime("%Z")
        if tz_str and tz_str.strip():
            tz_abbr = tz_str
        else:
            if time.tzname and len(time.tzname) > 0:
                tz_abbr = time.tzname[0] if time.daylight == 0 else time.tzname[1]
    except Exception:
        tz_abbr = "UTC"
    return dt.strftime(f"%d-%b-%Y_%H-%M-%S-{tz_abbr}")


def check_cluster_connectivity(
    context: str, retries: int, oc_timeout_seconds: int
) -> tuple[bool, str]:
    """Check cluster connectivity using appropriate method for the CLI tool."""
    cli_tool = detect_cli_tool()

    # oc has 'whoami', kubectl doesn't - use 'get ns' for kubectl
    if cli_tool == "oc":
        rc, out, err = run_cli_subcommand(
            context, ["whoami"], retries=retries, cli_timeout_seconds=oc_timeout_seconds
        )
    else:  # kubectl
        # Use 'get ns' as connectivity check (works for all auth methods)
        # Note: --request-timeout is already added by cli_cmd_parts, so we don't need it here
        rc, out, err = run_cli_subcommand(
            context, ["get", "ns"], retries=retries, cli_timeout_seconds=oc_timeout_seconds
        )

    if rc == 0:
        return True, ""
    return False, err or out or "unknown error"


def check_all_clusters_connectivity(
    contexts: list[str], retries: int, oc_timeout_seconds: int
) -> tuple[bool, list[tuple[str, bool, str]]]:
    """
    Check connectivity to all clusters.

    Returns:
        tuple: (all_connected, connectivity_report)
        - all_connected: True if all clusters are accessible
        - connectivity_report: List of (cluster_name, connected, error_message) tuples
    """
    report = []
    all_connected = True

    print(color("\n" + "=" * 80, BLUE))
    print(color("Checking Cluster Connectivity", BLUE))
    print(color("=" * 80, BLUE))

    for ctx in contexts:
        cluster = short_cluster_name(ctx)
        connected, error_msg = check_cluster_connectivity(
            ctx, retries=retries, oc_timeout_seconds=oc_timeout_seconds
        )
        if connected:
            report.append((cluster, True, "Connected"))
            print(color(f"  ✓ {cluster}: Connected", GREEN))
        else:
            report.append((cluster, False, error_msg))
            print(color(f"  ✗ {cluster}: {error_msg}", RED))
            all_connected = False

    print(color("=" * 80, BLUE))

    return all_connected, report


def print_connectivity_report_summary(connectivity_report: list[tuple[str, bool, str]]) -> None:
    """
    Print the Cluster Connectivity Report summary (second block).
    Does not prompt for user input.
    """
    print(color("\nCluster Connectivity Report:", BLUE))
    for cluster, connected, message in connectivity_report:
        if connected:
            print(color(f"  ✓ {cluster}: {message}", GREEN))
        else:
            print(color(f"  ✗ {cluster}: {message}", RED))

    all_connected = all(connected for _, connected, _ in connectivity_report)
    if all_connected:
        print(color("\n✓ All clusters are accessible", GREEN))
    else:
        print(color("\nWARNING: Some clusters are not accessible.", YELLOW))
        print(color("  Data collection may fail for these clusters.", YELLOW))
        print(color("  Continuing with accessible clusters only...", YELLOW))

    print(color("=" * 80, BLUE))


def parse_time_range(time_range_str: str) -> int:
    """
    Parse time range string (e.g., '1d', '2h', '30m', '1M') into seconds.
    Returns seconds from now to look back.
    """
    if not time_range_str:
        return 86400  # Default 1 day
    time_range_str = time_range_str.strip()
    # Do not lower: m=minutes, M=months (30 days)
    match = re.match(r"^(\d+)([smhdM])$", time_range_str)
    if not match:
        raise ValueError(f"Invalid time range format: {time_range_str}")
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "M": 2592000,  # 30 days
    }
    return value * multipliers.get(unit, 86400)


def get_all_events_oc(
    context: str,
    namespace: str,
    retries: int,
    oc_timeout_seconds: int,
    time_range_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """
    Get all events for a namespace (single API call for efficiency).

    Args:
        time_range_seconds: If provided, filter events to this time range
    """
    subcmd = ["-n", namespace, "get", "events", "--ignore-not-found", "-o", "json"]
    rc, out, err = run_oc_subcommand(
        context, subcmd, retries=retries, oc_timeout_seconds=oc_timeout_seconds
    )
    if rc != 0 or not out:
        return []
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as e:
        logging.warning(f"Failed to parse events JSON for {namespace}: {e}")
        return []
    events = obj.get("items", [])

    # Filter by time range if provided (Kubernetes event timestamps are UTC)
    if time_range_seconds:
        cutoff_time = datetime.now(UTC).timestamp() - time_range_seconds
        filtered_events = []
        for ev in events:
            ts = ev.get("eventTime") or ev.get("lastTimestamp") or ev.get("firstTimestamp")
            if ts:
                try:
                    # Parse as UTC and compare with cutoff
                    ev_ts = _parse_kubernetes_timestamp_utc(ts)
                    if ev_ts is not None and ev_ts >= cutoff_time:
                        filtered_events.append(ev)
                    elif ev_ts is None:
                        # Unparseable, include to be safe
                        filtered_events.append(ev)
                except (ValueError, AttributeError):
                    filtered_events.append(ev)
            else:
                filtered_events.append(ev)
        return filtered_events

    return events


def _application_component_from_labels(labels: dict[str, str] | None) -> tuple[str, str]:
    """Extract Application and Component from pod metadata.labels.

    Application: appstudio.openshift.io/application (Konflux), then standard Kubernetes labels.
    Component: tekton.dev/pipelineTask (Tekton step), tekton.dev/task, then standard labels.
    """
    if not labels:
        return "", ""
    application = (
        labels.get("appstudio.openshift.io/application")
        or labels.get("app.kubernetes.io/part-of")
        or labels.get("app.kubernetes.io/name")
        or labels.get("app")
        or ""
    ).strip()
    component = (
        labels.get("tekton.dev/pipelineTask")
        or labels.get("tekton.dev/task")
        or labels.get("app.kubernetes.io/component")
        or labels.get("component")
        or ""
    ).strip()
    return application, component


def get_pods_items(
    context: str,
    namespace: str,
    retries: int,
    oc_timeout_seconds: int,
) -> list[dict[str, Any]]:
    """Fetch pods in namespace as list of pod items.

    For reuse in OOM/Crash detection and labels.
    """
    subcmd = ["-n", namespace, "get", "pods", "-o", "json", "--ignore-not-found"]
    rc, out, err = run_oc_subcommand(
        context, subcmd, retries=retries, oc_timeout_seconds=oc_timeout_seconds
    )
    if rc != 0 or not out:
        return []
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as e:
        logging.warning(f"Failed to parse pods JSON for {namespace}: {e}")
        return []
    return obj.get("items", [])


def find_events_by_reason_oc(
    context: str,
    namespace: str,
    reason_substring: str,
    retries: int,
    oc_timeout_seconds: int,
    time_range_seconds: int | None = None,
) -> list[dict[str, str]]:
    """Find events matching a reason substring in a namespace."""
    events = get_all_events_oc(context, namespace, retries, oc_timeout_seconds, time_range_seconds)
    res: list[dict[str, str]] = []
    for ev in events:
        reason = ev.get("reason", "")
        if reason_substring.lower() not in reason.lower():
            continue
        pod = ev.get("involvedObject", {}).get("name")
        ts = ev.get("eventTime") or ev.get("lastTimestamp") or ev.get("firstTimestamp")
        if pod and ts:
            res.append({"pod": pod, "reason": reason, "timestamp": parse_timestamp_to_iso(ts)})
    return res


def oomkilled_via_pods_oc(
    context: str,
    namespace: str,
    retries: int,
    oc_timeout_seconds: int,
    time_range_seconds: int | None = None,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Find pods that were OOMKilled by querying pod status.

    Enhanced detection checks multiple states:
    - lastState.terminated.reason == "OOMKilled" (previous OOM kill)
    - state.terminated.reason == "OOMKilled" (current/just OOM killed)
    - Also checks initContainerStatuses for init container OOM kills

    When time_range_seconds is set, only include findings whose finishedAt
    is within the window (or include when finishedAt is missing).
    If items is provided (from get_pods_items), uses it and adds application/component from labels.
    """
    if items is None:
        items = get_pods_items(context, namespace, retries, oc_timeout_seconds)
    res: list[dict[str, str]] = []
    seen_pods: set[str] = set()  # Avoid duplicates with timestamps
    cutoff_time: float | None = None
    if time_range_seconds is not None:
        cutoff_time = datetime.now(UTC).timestamp() - time_range_seconds

    for item in items:
        pod_name = item.get("metadata", {}).get("name")
        if not pod_name:
            continue
        app, comp = _application_component_from_labels(item.get("metadata", {}).get("labels"))

        # Check both regular containers and init containers
        container_statuses = item.get("status", {}).get("containerStatuses", []) or []
        init_container_statuses = item.get("status", {}).get("initContainerStatuses", []) or []
        all_statuses = container_statuses + init_container_statuses

        for cs in all_statuses:
            # Check current state.terminated (just OOM killed)
            terminated = cs.get("state", {}).get("terminated", {})
            if terminated and terminated.get("reason") == "OOMKilled":
                finished_at = terminated.get("finishedAt", "")
                if cutoff_time is not None and not _timestamp_in_range(finished_at, cutoff_time):
                    continue
                key = f"{pod_name}:current"
                if key not in seen_pods:
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "OOMKilled",
                            "timestamp": (
                                parse_timestamp_to_iso(finished_at) if finished_at else ""
                            ),
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(key)
                continue

            # Check lastState.terminated.reason for OOMKilled (previous OOM kill)
            last_state = cs.get("lastState", {})
            last_terminated = last_state.get("terminated", {})
            if last_terminated and last_terminated.get("reason") == "OOMKilled":
                finished_at = last_terminated.get("finishedAt", "")
                if cutoff_time is not None and not _timestamp_in_range(finished_at, cutoff_time):
                    continue
                key = f"{pod_name}:last:{finished_at}"
                if key not in seen_pods:
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "OOMKilled",
                            "timestamp": (
                                parse_timestamp_to_iso(finished_at) if finished_at else ""
                            ),
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(key)

    return res


def crashloop_via_pods_oc(
    context: str,
    namespace: str,
    retries: int,
    oc_timeout_seconds: int,
    time_range_seconds: int | None = None,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Find pods in CrashLoopBackOff state by querying pod status.

    Enhanced detection checks multiple states:
    - state.waiting.reason == "CrashLoopBackOff" (current waiting state)
    - state.terminated.reason == "CrashLoopBackOff" (just crashed)
    - lastState.terminated.reason == "CrashLoopBackOff" (previous crash)
    - High restart count (restartCount > 0) as indicator of crash loops
    - Also checks initContainerStatuses for init container failures

    When time_range_seconds is set, only include findings that fall within the
    window. If we have a finishedAt timestamp, filter by it; if no timestamp,
    include the finding (don't drop due to missing metadata).
    If items is provided (from get_pods_items), uses it and adds application/component from labels.
    """
    if items is None:
        items = get_pods_items(context, namespace, retries, oc_timeout_seconds)
    res: list[dict[str, str]] = []
    seen_pods: set[str] = set()  # Avoid duplicates
    cutoff_time: float | None = None
    if time_range_seconds is not None:
        cutoff_time = datetime.now(UTC).timestamp() - time_range_seconds

    for item in items:
        pod_name = item.get("metadata", {}).get("name")
        if not pod_name:
            continue
        app, comp = _application_component_from_labels(item.get("metadata", {}).get("labels"))

        # Check both regular containers and init containers
        container_statuses = item.get("status", {}).get("containerStatuses", []) or []
        init_container_statuses = item.get("status", {}).get("initContainerStatuses", []) or []
        all_statuses = container_statuses + init_container_statuses

        for cs in all_statuses:
            # Check current state.waiting (no finishedAt; include if no time filter or by policy)
            waiting = cs.get("state", {}).get("waiting")
            if waiting and waiting.get("reason") == "CrashLoopBackOff":
                # No timestamp for waiting state; include when no time range or always include
                if pod_name not in seen_pods:
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "CrashLoopBackOff",
                            "timestamp": "",
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(pod_name)
                continue

            # Check current state.terminated (container just crashed)
            terminated = cs.get("state", {}).get("terminated")
            if terminated and terminated.get("reason") == "CrashLoopBackOff":
                finished_at = terminated.get("finishedAt", "")
                if cutoff_time is not None and not _timestamp_in_range(finished_at, cutoff_time):
                    continue
                if pod_name not in seen_pods:
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "CrashLoopBackOff",
                            "timestamp": parse_timestamp_to_iso(finished_at) if finished_at else "",
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(pod_name)
                continue

            # Check lastState.terminated (previous crash)
            last_state = cs.get("lastState", {})
            last_terminated = last_state.get("terminated", {})
            if last_terminated and last_terminated.get("reason") == "CrashLoopBackOff":
                finished_at = last_terminated.get("finishedAt", "")
                if cutoff_time is not None and not _timestamp_in_range(finished_at, cutoff_time):
                    continue
                if pod_name not in seen_pods:
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "CrashLoopBackOff",
                            "timestamp": parse_timestamp_to_iso(finished_at) if finished_at else "",
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(pod_name)
                continue

            # Check restart count as indicator of crash loops
            # Only flag if restart count is high (>= 3) AND there's evidence of crashes
            restart_count = cs.get("restartCount", 0)
            if restart_count >= 3:
                has_terminated_state = (
                    cs.get("state", {}).get("terminated") is not None
                    or cs.get("lastState", {}).get("terminated") is not None
                )
                if has_terminated_state and pod_name not in seen_pods:
                    # Use finishedAt from either state for time filter if available
                    finished_at = ""
                    term = cs.get("state", {}).get("terminated") or cs.get("lastState", {}).get(
                        "terminated"
                    )
                    if term:
                        finished_at = term.get("finishedAt", "")
                    if cutoff_time is not None and not _timestamp_in_range(
                        finished_at, cutoff_time
                    ):
                        continue
                    res.append(
                        {
                            "pod": pod_name,
                            "reason": "CrashLoopBackOff",
                            "timestamp": parse_timestamp_to_iso(finished_at) if finished_at else "",
                            "application": app,
                            "component": comp,
                        }
                    )
                    seen_pods.add(pod_name)
                    continue

        # Also check pod phase - Failed or Pending might indicate issues
        pod_phase = item.get("status", {}).get("phase", "")
        if pod_phase == "Failed" and pod_name not in seen_pods:
            has_restarts = any(cs.get("restartCount", 0) > 0 for cs in all_statuses)
            if has_restarts:
                # No specific finishedAt for phase Failed; include (no timestamp)
                res.append(
                    {
                        "pod": pod_name,
                        "reason": "CrashLoopBackOff",
                        "timestamp": "",
                        "application": app,
                        "component": comp,
                    }
                )
                seen_pods.add(pod_name)

    return res
