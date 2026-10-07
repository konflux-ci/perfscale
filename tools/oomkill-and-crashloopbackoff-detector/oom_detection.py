from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from oom_cluster import (
    _parse_kubernetes_timestamp_utc,
    _timestamp_in_range,
    parse_timestamp_to_iso,
    run_oc_subcommand,
)


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
