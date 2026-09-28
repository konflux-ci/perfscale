from __future__ import annotations

import contextlib
import glob
import logging
import re
from pathlib import Path

from oom_cluster import (
    color,
    now_ts_for_filename,
    run_oc_subcommand,
    timestamp_for_backup_from_file,
)
from oom_constants import (
    YELLOW,
)


def save_pod_artifacts(
    context: str,
    cluster: str,
    namespace: str,
    pod: str,
    retries: int,
    oc_timeout_seconds: int,
    artifacts_root: Path,
) -> tuple[str, str]:
    """
    Save 'oc describe pod' and pod logs into files under artifacts_root/<cluster>/.
    Log file contains: first --previous (crashed container), then current logs, in one file.
    Filenames include namespace, pod name and timestamp to avoid collisions.
    Returns absolute file paths (description_file, pod_log_file).
    """
    ts = now_ts_for_filename()
    cluster_dir = (artifacts_root / cluster).resolve()
    cluster_dir.mkdir(parents=True, exist_ok=True)

    # safe filename parts
    ns_safe = re.sub(r"[^A-Za-z0-9_.-]", "_", namespace)
    pod_safe = re.sub(r"[^A-Za-z0-9_.-]", "_", pod)

    desc_fname = f"{ns_safe}__{pod_safe}__{ts}__desc.txt"
    log_fname = f"{ns_safe}__{pod_safe}__{ts}__log.txt"

    desc_path = cluster_dir / desc_fname
    log_path = cluster_dir / log_fname

    # oc describe pod
    try:
        rc, out, err = run_oc_subcommand(
            context,
            ["-n", namespace, "describe", "pod", pod],
            retries=retries,
            oc_timeout_seconds=oc_timeout_seconds,
        )
        content_desc = (
            out if rc == 0 and out else (err if err else "Failed to fetch pod description")
        )
    except Exception as e:
        logging.error(f"Error fetching pod description for {namespace}/{pod}: {e}")
        content_desc = f"Error fetching pod description: {e}"

    try:
        desc_path.write_text(content_desc)
    except Exception as e:
        # fallback to best-effort path
        desc_path = cluster_dir / f"{ns_safe}__{pod_safe}__{ts}__desc.failed.txt"
        with contextlib.suppress(Exception):
            desc_path.write_text(
                f"Failed to write description: {e}\nOriginal content:\n{content_desc}"
            )

    # oc logs: --previous first (crashed container), then current; append both to one file
    log_sections: list[str] = []
    try:
        # 1. Previous container logs (from the run that OOM'd/crashed)
        rc_prev, out_prev, err_prev = run_oc_subcommand(
            context,
            ["-n", namespace, "logs", pod, "--previous"],
            retries=retries,
            oc_timeout_seconds=oc_timeout_seconds,
        )
        prev_content = out_prev if rc_prev == 0 and out_prev else (err_prev or "(no previous logs)")
        log_sections.append(
            "=== Previous container logs (oc logs <pod> --previous) ===\n" + prev_content
        )
        # 2. Current container logs
        rc_cur, out_cur, err_cur = run_oc_subcommand(
            context,
            ["-n", namespace, "logs", pod],
            retries=retries,
            oc_timeout_seconds=oc_timeout_seconds,
        )
        cur_content = out_cur if rc_cur == 0 and out_cur else (err_cur or "(no current logs)")
        log_sections.append("=== Current container logs (oc logs <pod>) ===\n" + cur_content)
        log_content = "\n\n".join(log_sections)
    except Exception as e:
        logging.error(f"Error fetching logs for {namespace}/{pod}: {e}")
        log_content = f"Error fetching logs: {e}"

    try:
        log_path.write_text(log_content)
    except Exception as e:
        log_path = cluster_dir / f"{ns_safe}__{pod_safe}__{ts}__log.failed.txt"
        with contextlib.suppress(Exception):
            log_path.write_text(f"Failed to write logs: {e}\nOriginal logs content:\n{log_content}")

    return str(desc_path.resolve()), str(log_path.resolve())


def is_artifact_meaningful(content: str) -> bool:
    """
    Check if artifact content is meaningful (not just 'pod not found' errors).

    Uses a two-stage approach:
    1. Size check: If content is reasonably large (>2KB), assume it's meaningful
    2. Pattern check: For small content, look for specific 'oc' error patterns

    Returns:
        True if content has useful information, False if pod was deleted/not found
    """
    if not content or not content.strip():
        return False

    # Stage 1: Size-based heuristic
    # If content is larger than 2KB, it's likely meaningful (not just an error message)
    MEANINGFUL_SIZE_THRESHOLD = 2048  # 2KB
    content_size = len(content.encode("utf-8"))

    if content_size >= MEANINGFUL_SIZE_THRESHOLD:
        return True

    # Stage 2: Pattern matching for small content (< 2KB)
    # Only apply strict error pattern checks on small files
    lower = content.lower()
    lines = content.strip().split("\n")
    num_lines = len(lines)

    # Small files (< 10 lines) with specific 'oc' error patterns are likely not meaningful
    if num_lines < 10:
        # Check for exact "Error from server (NotFound): pods "..." not found" pattern
        if "error from server" in lower and "notfound" in lower and "pods" in lower:
            return False
        # Check for "Error from server: pods "..." not found" pattern
        if "error from server" in lower and "not found" in lower and "pods" in lower:
            return False
        # Check for standalone "pod not found" errors
        for line in lines:
            line_lower = line.lower().strip()
            if line_lower.startswith("error") and "pod" in line_lower and "not found" in line_lower:
                return False

    # If we got here, either:
    # - Content is between 10 lines and 2KB (likely meaningful)
    # - Or no error patterns matched
    return True


def ensure_output_directory(path_str: str = "output") -> Path:
    """
    Ensure the output subdirectory exists, creating it if necessary.
    Also ensures the tarballs/ subdir exists (for oom_logs_and_desc_bundle_generator
    when run from Jenkins or locally, so tarballs do not clutter the main output dir).

    Uses mkdir(parents=True, exist_ok=True) (equivalent to 'mkdir -p'): creates
    dirs only when missing; never removes or truncates existing dirs, so historical
    data is preserved across runs.

    Returns:
        Path to the output directory
    """
    output_dir = Path(path_str)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tarballs").mkdir(parents=True, exist_ok=True)
    return output_dir


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
