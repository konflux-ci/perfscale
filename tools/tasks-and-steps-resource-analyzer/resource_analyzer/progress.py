"""Terminal progress spinner helpers."""

import shutil
import sys
import time
from threading import Lock
from typing import Any

_PROGRESS_IO_LOCK = Lock()
_MAX_ACTIVE_CLUSTERS_IN_SPINNER = 2


def _terminal_width() -> Any:
    """Best-effort terminal width for truncating in-place progress lines."""
    try:
        return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
    except Exception:
        return 80


def _truncate_progress_line(message) -> Any:
    """Fit message on one terminal row so \\r can overwrite it cleanly."""
    max_len = max(20, _terminal_width() - 1)
    if len(message) <= max_len:
        return message
    return message[: max_len - 1] + "…"


def _progress_overwrite(message) -> Any:
    """Update the current progress line in place (no newline)."""
    message = _truncate_progress_line(message)
    with _PROGRESS_IO_LOCK:
        sys.stderr.write(f"\r\033[K{message}")
        sys.stderr.flush()


def _progress_milestone(message) -> Any:
    """Print a one-line milestone (e.g. cluster checkpoint), then free the line for the spinner.

    Clears any in-progress spinner row first so \\r overwrite cannot leave wrapped junk.
    """
    with _PROGRESS_IO_LOCK:
        sys.stderr.write(f"\r\033[K{message}\n")
        sys.stderr.flush()


def _spinner_thread(stop_event, progress_data=None, progress_lock=None, total_clusters=0) -> Any:
    """Display a spinning wheel with percentage progress while collecting data from clusters.

    Shows overall cluster completion percentage plus a live pod-progress counter for each
    cluster that is currently being processed, so operators can distinguish a slow cluster
    from a truly stuck one.

    The status line is overwritten in place (\\\\r). Milestone events (checkpoints) print on
    their own line via _progress_milestone so they do not break the spinner.
    """
    spinner_chars = ["|", "/", "-", "\\"]
    idx = 0
    while not stop_event.is_set():
        percentage = 0
        completed_count = 0
        pods_listed = 0
        pods_kept = 0
        active_parts = []
        if progress_data and total_clusters > 0 and progress_lock:
            with progress_lock:
                completed_count = len(progress_data.get("completed", []))
                done_set = set(progress_data.get("completed", []))
                pods_listed = progress_data.get("pods_listed", 0)
                pods_kept = progress_data.get("pods_kept", 0)
                # Build per-cluster pod progress from live stats references
                for cname, info in progress_data.get("active_clusters", {}).items():
                    if cname in done_set:
                        continue
                    # stats_ref is the cluster worker's own stats dict (shared reference,
                    # no lock needed for reading — GIL makes individual dict reads safe here)
                    done_n = info["stats_ref"].get("pods_queried", 0)
                    total_n = info["total"]
                    active_parts.append(f"{cname}:{done_n}/{total_n}")
            percentage = int((completed_count / total_clusters) * 100)
        spin = spinner_chars[idx % len(spinner_chars)]
        # Cap how many active clusters we show so the line stays short enough to overwrite.
        if active_parts:
            shown = active_parts[:_MAX_ACTIVE_CLUSTERS_IN_SPINNER]
            extra = len(active_parts) - len(shown)
            active_body = "  ".join(shown)
            if extra > 0:
                active_body += f" +{extra} more"
            active_str = f"  active→[{active_body}]"
        else:
            active_str = ""
        if percentage > 0:
            message = (
                f"Clusters: {percentage}% ({completed_count}/{total_clusters} done)"
                f"{active_str}"
                f"  [listed={pods_listed} kept={pods_kept}] {spin}"
            )
        else:
            message = (
                f"Clusters: starting...{active_str}  [listed={pods_listed} kept={pods_kept}] {spin}"
            )
        _progress_overwrite(message)
        idx += 1
        time.sleep(0.2)
    with _PROGRESS_IO_LOCK:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
