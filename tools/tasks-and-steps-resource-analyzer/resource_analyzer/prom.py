"""Prometheus lookback helpers and HTTP client (collection lives in prom_collect)."""

import json
import re
import subprocess
import sys
from collections import defaultdict
from typing import Any

from .paths import TOOL_DIR

try:
    import requests  # noqa: F401 — required at runtime; used via Session in callers
    import urllib3
except ImportError:
    print(
        "Error: Missing required library. Install with: pip install requests pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def format_promql_duration(seconds) -> Any:
    """Format a lookback window for PromQL range selectors (e.g. 1d, 6h, 90m)."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def format_lookback_label(days, hours) -> Any:
    """Human-readable lookback like '7d', '6h', or '1d+6h'."""
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    return "+".join(parts) if parts else "0"


def resolve_lookback_seconds(days, hours) -> Any:
    """Combine --days and --hours into a total lookback in seconds."""
    days = int(days or 0)
    hours = int(hours or 0)
    if days < 0 or hours < 0:
        raise ValueError("--days and --hours must be >= 0")
    total = days * 86400 + hours * 3600
    if total <= 0:
        raise ValueError("Lookback window must be > 0 (use --days and/or --hours)")
    return total


def _empty_collection_counters() -> Any:
    return {
        "pods_listed": 0,
        "pods_queried": 0,
        "pods_kept": 0,
        "query_failures": 0,
        "empty_metrics": 0,
        "parse_errors": 0,
        "list_failures": 0,
        "http_queries": 0,
    }


# Transport batch size for pod=~"(a|b|...)" PromQL (matches wrapper_for_promql.sh).
POD_BATCH_SIZE = 50

_COMPONENT_LABEL_KEYS = (
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
)

_APPLICATION_LABEL_KEYS = (
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
)


def _merge_counters(dest, src) -> Any:
    for key, value in src.items():
        dest[key] = dest.get(key, 0) + value


DEBUG_SKIP_SAMPLE_LIMIT = 15


def _first_label_present(mapping, keys) -> Any:
    """Return the first non-empty label value from mapping for the given keys."""
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return "N/A"


def _escape_promql_regex(value) -> Any:
    """Escape a literal for PromQL regex inside a double-quoted matcher.

    PromQL double-quoted strings use Go-style escapes, so a regex metacharacter
    escape must appear as two backslashes in the query text (e.g. ``\\\\.`` for
    ``.``). A single backslash would be an unknown escape and Prometheus rejects
    the query with HTTP 400.
    """
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\\\1", value)


def _pod_regex_for_batch(pod_names) -> Any:
    """Build an alternation regex for a batch of pod names."""
    return "|".join(_escape_promql_regex(p) for p in pod_names if p)


def _series_peak_and_first_ts(series) -> Any:
    """Return (peak_value, first_timestamp) from an instant or range series."""
    if not isinstance(series, dict):
        return 0.0, None
    if "value" in series and series["value"]:
        ts, val = series["value"]
        try:
            peak = float(val) if val not in (None, "") else 0.0
        except (TypeError, ValueError):
            peak = 0.0
        try:
            first_ts = float(ts) if ts not in (None, "") else None
        except (TypeError, ValueError):
            first_ts = None
        return peak, first_ts

    peak = 0.0
    first_ts = None
    for ts, val in series.get("values") or []:
        if first_ts is None and ts not in (None, ""):
            try:
                first_ts = float(ts)
            except (TypeError, ValueError):
                first_ts = None
        try:
            v = float(val) if val not in (None, "") else 0.0
        except (TypeError, ValueError):
            v = 0.0
        if v > peak:
            peak = v
    return peak, first_ts


def _peaks_by_pod(prom_response) -> Any:
    """Map pod name -> (peak, first_ts) from a Prometheus instant/range response."""
    out: dict[str, tuple[Any, Any]] = {}
    if not isinstance(prom_response, dict):
        return out
    for series in prom_response.get("data", {}).get("result", []) or []:
        pod = (series.get("metric") or {}).get("pod") or ""
        if not pod:
            continue
        peak, first_ts = _series_peak_and_first_ts(series)
        prev = out.get(pod)
        if prev is None or peak > prev[0]:
            out[pod] = (peak, first_ts if first_ts is not None else (prev[1] if prev else None))
        elif prev is not None and prev[1] is None and first_ts is not None:
            out[pod] = (prev[0], first_ts)
    return out


def _component_fallback_from_names(pod_name, namespace) -> Any:
    """Best-effort component from namespace/pod name when labels are missing."""
    if namespace and namespace != "N/A" and namespace.endswith("-tenant"):
        potential = namespace[:-7]
        if potential:
            return potential, "N/A"
    if pod_name:
        parts = pod_name.split("-")
        if len(parts) >= 2 and len(parts[0]) > 1:
            return parts[0], "N/A"
    return "N/A", "N/A"


def _query_prometheus_instant(
    session, host, token, query, eval_time=None, timeout=900, sem=None
) -> Any:
    """Query Prometheus /api/v1/query (instant); returns response JSON dict."""
    url = f"https://{host}/api/v1/query"
    params = {"query": query}
    if eval_time is not None:
        params["time"] = eval_time
    if sem is not None:
        sem.acquire()
    try:
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            verify=False,  # nosec B501
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        if sem is not None:
            sem.release()


def _query_prometheus_range(session, host, token, query, start, end, timeout=900, sem=None) -> Any:
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
    if sem is not None:
        sem.acquire()
    try:
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"query": query, "start": start, "end": end, "step": step},
            verify=False,  # nosec B501
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    finally:
        if sem is not None:
            sem.release()


def _list_task_pods(
    session, host, token, task_name, end_time_secs, lookback_seconds, sem=None
) -> Any:
    """List pods for a task via Prometheus kube_pod_labels; returns response JSON dict."""
    if lookback_seconds <= 0:
        lookback_seconds = 86400
    step = max(15, lookback_seconds // 5760)
    if sem is not None:
        sem.acquire()
    try:
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
    finally:
        if sem is not None:
            sem.release()


def _get_component_for_pod(session, host, token, pod, namespace, end_time, days, sem=None) -> Any:
    """Get component/application labels from Prometheus kube_pod_labels.

    Returns (component, application) strings; each defaults to "N/A".
    """

    def _fetch(query, use_range) -> Any:
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
        if sem is not None:
            sem.acquire()
        try:
            resp = session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                verify=False,  # nosec B501
                timeout=30,
            )
        finally:
            if sem is not None:
                sem.release()
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
    return (
        _first_label_present(metric, _COMPONENT_LABEL_KEYS),
        _first_label_present(metric, _APPLICATION_LABEL_KEYS),
    )


def _fill_component_cache_for_pods(
    session, host, token, pods, end_time, lookback_seconds, component_cache, sem=None
) -> Any:
    """Batch-fill component/application cache for pods still missing labels.

    Uses query_range over the full lookback window (not an instant query at
    end_time) so historical pods that finished earlier still resolve labels.

    pods: iterable of (pod_name, namespace). Mutates component_cache in place.
    Returns the number of Prometheus HTTP queries issued.
    """
    missing_by_ns = defaultdict(list)
    for pod_name, namespace in pods:
        key = (pod_name, namespace or "")
        comp, _app = component_cache.get(key, ("N/A", "N/A"))
        if not comp or comp == "N/A":
            missing_by_ns[namespace or ""].append(pod_name)

    try:
        end_ts = int(end_time)
        start_ts = end_ts - int(lookback_seconds or 86400)
    except (TypeError, ValueError):
        end_ts = None
        start_ts = None

    http_queries = 0
    for namespace, pod_list in missing_by_ns.items():
        ns_valid = bool(namespace and namespace != "N/A")
        for i in range(0, len(pod_list), POD_BATCH_SIZE):
            batch = pod_list[i : i + POD_BATCH_SIZE]
            regex = _pod_regex_for_batch(batch)
            if not regex:
                continue
            if ns_valid:
                query = f'kube_pod_labels{{namespace="{namespace}",pod=~"({regex})"}}'
            else:
                query = f'kube_pod_labels{{pod=~"({regex})"}}'
            try:
                if start_ts is not None and end_ts is not None:
                    resp = _query_prometheus_range(
                        session,
                        host,
                        token,
                        query,
                        start_ts,
                        end_ts,
                        timeout=60,
                        sem=sem,
                    )
                else:
                    resp = _query_prometheus_instant(
                        session, host, token, query, eval_time=end_time, timeout=60, sem=sem
                    )
                http_queries += 1
            except Exception:  # nosec B110
                http_queries += 1
                continue
            for series in resp.get("data", {}).get("result", []) or []:
                metric = series.get("metric") or {}
                pod = metric.get("pod") or ""
                ns = metric.get("namespace") or namespace or ""
                if not pod:
                    continue
                comp = _first_label_present(metric, _COMPONENT_LABEL_KEYS)
                app = _first_label_present(metric, _APPLICATION_LABEL_KEYS)
                key = (pod, ns)
                prev_comp, prev_app = component_cache.get(key, ("N/A", "N/A"))
                if comp != "N/A" or prev_comp == "N/A":
                    component_cache[key] = (
                        comp if comp != "N/A" else prev_comp,
                        app if app != "N/A" else prev_app,
                    )
    return http_queries


def extract_component_from_pod(
    pod_name, namespace, token, prom_host, end_time, days, session=None
) -> Any:
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
            script_dir = TOOL_DIR
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

        return _component_fallback_from_names(pod_name, namespace)
    except Exception as e:
        if globals().get("args") and globals()["args"].debug:
            print(
                f"DEBUG: Error extracting component for pod {pod_name}: {e}",
                file=sys.stderr,
            )
        return ("N/A", "N/A")
