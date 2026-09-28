from __future__ import annotations

import atexit
import csv
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from oom_constants import (
    _CODEOWNERS_ATEXIT_REGISTERED,
    _CODEOWNERS_TEMP_DIR,
    _MONTH_ABBR_TO_NUM,
    BLUE,
    KONFLUX_RELEASE_DATA_REPO,
    YELLOW,
)
from oom_lib_0 import color


def _pod_base_name(full_name: str) -> str:
    """
    Derive a readable, collatable base pod name by replacing hashes and random IDs
    with '*', so multiple pods group under one report (e.g. CI jobs, hostnames).
    Examples:
      backfill-redis-v1-2-on-pull-request-g6w9f-run-unit-test -> ...-*-run-unit-test
      kube-rbac-proxy-crio-ip-10-202-25-219.ec2.internal -> ...-ip-*.internal
      instance-6xsb9 -> instance
      gatekeeper-op41130a155... -> gatekeeper-op
    """
    if not full_name:
        return full_name
    name = full_name

    # 1. Hostname: *-ip-<something>.ec2.internal or *.internal -> *-ip-*.internal
    if ".internal" in name and "-ip-" in name:
        idx = name.find("-ip-")
        inr = name.find(".internal")
        if idx >= 0 and inr > idx:
            name = name[: idx + 4] + "*" + name[inr:]

    # 2. instance-<short single segment> -> instance
    if name.startswith("instance-"):
        rest = name[len("instance-") :]
        if rest.isalnum() and len(rest) <= 10 and "-" not in rest:
            return "instance"

    # 3. Split by '-' for segment-wise rules (rejoin later)
    segments = name.split("-")
    out: list[str] = []

    for _i, seg in enumerate(segments):
        if not seg:
            out.append(seg)
            continue
        # Segment with a dot (e.g. 219.ec2.internal) - keep as-is or already handled
        if "." in seg:
            out.append(seg)
            continue
        # Short word + long alnum (e.g. op41130..., observ1b4c..., pullca107...)
        # -> word only (check before long-hash)
        if len(seg) > 10 and seg.isalnum():
            # Try known CI/word prefixes first (so "pull" wins over
            # "pullca"); no "pu" so pu<hash> -> *
            for prefix in ("pull", "reque", "observ", "op", "midstream", "on"):
                if seg.startswith(prefix) and len(seg) > len(prefix) + 12:
                    out.append(prefix)
                    break
            else:
                # Longest all-alpha prefix followed by 12+ chars (the hash)
                word_len = 0
                for j, c in enumerate(seg):
                    if c.isalpha():
                        word_len = j + 1
                    else:
                        break
                if word_len >= 2 and word_len < len(seg) and len(seg) - word_len >= 12:
                    word = seg[:word_len]
                    # "pu" + long hash -> * so trailing -* gets
                    # dropped (e.g. cloudwatch-aggregator-on)
                    if word == "pu" and len(seg) - word_len >= 20:
                        out.append("*")
                    else:
                        out.append(word)
                elif len(seg) >= 20:
                    # No alpha prefix (e.g. t98022b86..., a08677e97...); treat as hash
                    out.append("*")
                else:
                    out.append(seg)
            continue
        # Long hash segment (20+ alnum) with no alpha prefix -> *
        if len(seg) >= 20 and seg.isalnum():
            out.append("*")
            continue
        # Short random-looking ID (5-8 alnum, contains digit) -> *
        if 5 <= len(seg) <= 8 and seg.isalnum() and any(c.isdigit() for c in seg):
            out.append("*")
            continue
        # ReplicaSet-style hash (8-10 alnum, contains digit) as standalone segment -> *
        if 8 <= len(seg) <= 10 and seg.isalnum() and any(c.isdigit() for c in seg):
            out.append("*")
            continue
        out.append(seg)

    # 4. Collapse consecutive '*' into one
    collapsed: list[str] = []
    for s in out:
        if s == "*" and collapsed and collapsed[-1] == "*":
            continue
        collapsed.append(s)
    result = "-".join(collapsed)

    # 5. Drop trailing lone '*' or *-only suffix (e.g. odh-midstream-* -> odh-midstream)
    while result.endswith("-*") and result.count("-") > 1:
        result = result[:-2]

    # 6. Classic ReplicaSet: <name>-<hash>-<suffix> if we still have *-* at end, keep one *
    if result.endswith("-*-*"):
        result = result[:-2]  # remove last -*

    # 7. Trailing "-pod" (CI job pod suffix)
    if result.endswith("-pod") and result.count("-") > 1:
        result = result[:-4]

    # 8. Trailing short random-looking segment (5-8 alnum, e.g. -wzpwf, -bjcvs)
    # -> * (keep words like verify, apply)
    _keep_trailing = frozenset(
        (
            "verify",
            "apply",
            "build",
            "push",
            "pull",
            "scan",
            "test",
            "tags",
            "pod",
            "run",
            "tekton",
            "check",
            "observ",
            "dependencies",
            "unicode",
        )
    )
    while result.count("-") >= 1:
        last_part = result.rsplit("-", 1)[-1]
        if (
            5 <= len(last_part) <= 8
            and last_part.isalnum()
            and last_part.lower() not in _keep_trailing
        ):
            result = result[: -len(last_part) - 1] + "-*"
            while result.endswith("-*") and result.count("-") > 1:
                result = result[:-2]
            break
        break

    return result if result else full_name


def _match_string_for_bundle_generator(pod_names: list[str]) -> str:
    """
    Return a substring that matches all given pod names in CSV column 3.
    oom_logs_and_desc_bundle_generator uses index($3, pod) > 0, so we need a
    literal string that appears in the actual pod names (not the display base name
    with asterisks). Use longest common prefix so we match exactly this group.
    """
    if not pod_names:
        return ""
    if len(pod_names) == 1:
        return pod_names[0]
    prefix = pod_names[0]
    for name in pod_names[1:]:
        i = 0
        for a, b in zip(prefix, name, strict=False):
            if a != b:
                break
            i += 1
        prefix = prefix[:i]
    # Strip trailing hyphen so we match "apiserver-69cc49fdf9" in "apiserver-69cc49fdf9-cbnj4"
    return prefix.rstrip("-") if prefix else pod_names[0]


def _date_from_timestamped_csv_basename(basename: str) -> str | None:
    """Extract DD-Mon-YYYY from oom_results_DD-Mon-YYYY_*.csv. Returns None if not matched."""
    if not basename.startswith("oom_results_") or not basename.endswith(".csv"):
        return None
    # oom_results_03-Feb-2026_12-04-19-EDT.csv -> 03-Feb-2026
    m = re.match(r"oom_results_(\d{2}-[A-Za-z]{3}-\d{4})_[^.]*\.csv", basename)
    return m.group(1) if m else None


def _label_from_timestamped_csv_basename(basename: str) -> str | None:
    """Extract display label DD-Mon-YYYY HH:MM from oom_results_DD-Mon-YYYY_HH-MM-SS-TZ.csv."""
    if not basename.startswith("oom_results_") or not basename.endswith(".csv"):
        return None
    # oom_results_03-Feb-2026_12-04-19-EDT.csv -> 03-Feb-2026 12:04
    m = re.match(
        r"oom_results_(\d{2}-[A-Za-z]{3}-\d{4})_(\d{2})-(\d{2})-(\d{2})-[^.]*\.csv", basename
    )
    if not m:
        return _date_from_timestamped_csv_basename(basename)  # fallback to date only
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}"


def _run_date_from_timestamped_csv_basename(basename: str) -> date | None:
    """Parse run date (DD-Mon-YYYY) from filename to a date.

    For filtering/sorting. Locale-independent.
    """
    date_str = _date_from_timestamped_csv_basename(basename)
    if not date_str:
        return None
    try:
        # Locale-independent: DD-Mon-YYYY (e.g. 22-Jan-2026)
        parts = date_str.split("-")
        if len(parts) != 3:
            return None
        dd = int(parts[0])
        mon = _MONTH_ABBR_TO_NUM.get(parts[1].lower())
        yyyy = int(parts[2])
        if mon is None or dd < 1 or dd > 31 or yyyy < 2000 or yyyy > 2100:
            return None
        return date(yyyy, mon, dd)
    except (ValueError, TypeError):
        return None


def build_historical_series_by_cluster_from_output_dir(
    output_dir: Path,
    plot_range_seconds: int,
) -> dict[str, list[tuple[str, int, int]]]:
    """
    Build per-cluster historical (label, oom_count, crash_count) from timestamped CSVs.
    Same cutoff logic as build_historical_series_from_output_dir. Returns dict cluster -> list
    of (label, oom, crash) for runs in plot range where that cluster had data.
    """
    resolved_dir = output_dir.resolve()
    files = sorted(resolved_dir.glob("oom_results_*_*.csv"))
    # Collect per (run_date, label) per-cluster counts
    run_data: list[tuple[str, date, dict[str, tuple[int, int]]]] = []
    for path in files:
        try:
            run_date = _run_date_from_timestamped_csv_basename(path.name)
            if run_date is None:
                run_date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
            label = _label_from_timestamped_csv_basename(path.name) or path.name
            cluster_counts: dict[str, tuple[int, int]] = {}
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cluster = (row.get("cluster") or "").strip() or "unknown"
                    if cluster not in cluster_counts:
                        cluster_counts[cluster] = (0, 0)
                    oom, crash = cluster_counts[cluster]
                    t = _normalize_type(row.get("type", ""))
                    if t == "OOMKilled":
                        oom += 1
                    elif t == "CrashLoopBackOff":
                        crash += 1
                    cluster_counts[cluster] = (oom, crash)
            run_data.append((label, run_date, cluster_counts))
        except (OSError, csv.Error) as e:
            logging.debug(f"Skip {path.name}: {e}")
            continue
    # Include current run (oom_results.csv) so today's run appears on per-cluster graphs
    main_csv = resolved_dir / "oom_results.csv"
    if main_csv.is_file():
        try:
            mtime = main_csv.stat().st_mtime
            run_date = datetime.fromtimestamp(mtime, tz=UTC).date()
            label = datetime.fromtimestamp(mtime).strftime(
                "%d-%b-%Y %H:%M"
            )  # local time for display
            cluster_counts = {}
            with main_csv.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cluster = (row.get("cluster") or "").strip() or "unknown"
                    if cluster not in cluster_counts:
                        cluster_counts[cluster] = (0, 0)
                    oom, crash = cluster_counts[cluster]
                    t = _normalize_type(row.get("type", ""))
                    if t == "OOMKilled":
                        oom += 1
                    elif t == "CrashLoopBackOff":
                        crash += 1
                    cluster_counts[cluster] = (oom, crash)
            run_data.append((label, run_date, cluster_counts))
        except (OSError, csv.Error) as e:
            logging.debug(f"Skip {main_csv.name}: {e}")
    if not run_data:
        return {}
    latest = max(rd[1] for rd in run_data)
    cutoff_ts = (
        datetime.combine(latest, datetime.min.time()).replace(tzinfo=UTC).timestamp()
        - plot_range_seconds
    )
    cutoff_date = datetime.fromtimestamp(cutoff_ts, tz=UTC).date()
    # Build cluster -> list of (label, oom, crash) for runs in range
    by_cluster: dict[str, list[tuple[str, int, int, date]]] = {}
    for label, run_date, cluster_counts in run_data:
        if run_date < cutoff_date:
            continue
        for cluster, (oom, crash) in cluster_counts.items():
            if cluster not in by_cluster:
                by_cluster[cluster] = []
            by_cluster[cluster].append((label, oom, crash, run_date))
    for cluster in by_cluster:
        by_cluster[cluster].sort(key=lambda x: (x[3], x[0]))
    return {
        cluster: [(label, oom, crash) for label, oom, crash, _ in by_cluster[cluster]]
        for cluster in by_cluster
    }


def get_historical_html_links(output_dir: Path) -> list[tuple[str, str]]:
    """
    Return list of (label, filename) for timestamped oom_results_*_*.html in output_dir,
    sorted by run date descending (most recent first). Use relative filename so links work with file://.
    """
    resolved_dir = output_dir.resolve()
    candidates: list[tuple[date, str, str]] = []
    for path in resolved_dir.glob("oom_results_*_*.html"):
        # Reuse CSV basename helpers by pretending .html is .csv for date/label parsing
        fake_csv_name = path.name.replace(".html", ".csv")
        run_date = _run_date_from_timestamped_csv_basename(fake_csv_name)
        if run_date is None:
            run_date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
        label = _label_from_timestamped_csv_basename(fake_csv_name) or path.stem
        candidates.append((run_date, label, path.name))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(label, filename) for (_, label, filename) in candidates]


def _normalize_type(t: str) -> str:
    """Normalize type to OOMKilled or CrashLoopBackOff."""
    u = (t or "").strip().lower()
    if u == "oomkilled":
        return "OOMKilled"
    if u == "crashloopbackoff":
        return "CrashLoopBackOff"
    return (t or "").strip()


def _read_csv_rows_with_date(csv_path: Path, date_str: str) -> list[dict[str, str]]:
    """Read CSV and return list of row dicts with all columns (cluster, namespace, pod, type,
    application, component, timestamps, sources, description_file, pod_log_file, time_range, date).
    Preserves full row so HTML/details table and summaries get all fields. Old CSVs without
    application/component columns get empty strings."""
    rows: list[dict[str, str]] = []
    try:
        with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pod = (row.get("pod") or "").strip()
                if not pod:
                    continue
                raw_type = (row.get("type") or "").strip()
                out = {
                    "cluster": (row.get("cluster") or "").strip(),
                    "namespace": (row.get("namespace") or "").strip(),
                    "pod": pod,
                    "type": _normalize_type(raw_type),
                    "application": (row.get("application") or "").strip(),
                    "component": (row.get("component") or "").strip(),
                    "timestamps": (row.get("timestamps") or "").strip(),
                    "sources": (row.get("sources") or "").strip(),
                    "description_file": (row.get("description_file") or "").strip(),
                    "pod_log_file": (row.get("pod_log_file") or "").strip(),
                    "time_range": (row.get("time_range") or "").strip(),
                    "date": date_str,
                }
                rows.append(out)
    except OSError as e:
        logging.warning(f"Failed to read CSV {csv_path}: {e}")
    return rows


def _load_historical_rows_from_output_dir(output_dir: Path) -> list[dict[str, str]]:
    """Load rows from all timestamped oom_results_*_*.csv in output_dir (date from filename)."""
    historical: list[dict[str, str]] = []
    for path in sorted(output_dir.glob("oom_results_*_*.csv")):
        date_str = _date_from_timestamped_csv_basename(path.name)
        if date_str:
            historical.extend(_read_csv_rows_with_date(path, date_str))
    return historical


def _cleanup_codeowners_temp_dir() -> None:
    global _CODEOWNERS_TEMP_DIR
    if _CODEOWNERS_TEMP_DIR:
        shutil.rmtree(_CODEOWNERS_TEMP_DIR, ignore_errors=True)
        _CODEOWNERS_TEMP_DIR = None


def resolve_codeowners_dir(cli_arg: str | None) -> Path | None:
    """
    If cli_arg points to an existing directory, use it as konflux-release-data.
    Otherwise shallow-clone KONFLUX_RELEASE_DATA_REPO to a temp directory (cleaned on exit)
    and return that path. Returns None if clone fails or cli_arg is a non-directory path.
    """
    global _CODEOWNERS_TEMP_DIR, _CODEOWNERS_ATEXIT_REGISTERED

    if cli_arg and cli_arg.strip():
        p = Path(cli_arg.strip()).expanduser().resolve()
        if p.is_dir():
            return p
        print(color(f"WARNING: --codeowners-dir is not a directory: {p}", YELLOW))
        return None

    if _CODEOWNERS_TEMP_DIR:
        cached = Path(_CODEOWNERS_TEMP_DIR)
        if cached.is_dir():
            return cached

    tmp = tempfile.mkdtemp(prefix="konflux-release-data-")
    try:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "-q", KONFLUX_RELEASE_DATA_REPO, tmp],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip() or "git clone failed"
            logging.warning("CODEOWNERS: could not clone konflux-release-data: %s", detail)
            shutil.rmtree(tmp, ignore_errors=True)
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logging.warning("CODEOWNERS: clone konflux-release-data failed: %s", e)
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    _CODEOWNERS_TEMP_DIR = tmp
    if not _CODEOWNERS_ATEXIT_REGISTERED:
        atexit.register(_cleanup_codeowners_temp_dir)
        _CODEOWNERS_ATEXIT_REGISTERED = True
    print(
        color(
            f"Cloned konflux-release-data for CODEOWNERS lookups (temp: {tmp})",
            BLUE,
        )
    )
    return Path(tmp)


def _get_owners_for_namespace(codeowners_dir: Path, cluster: str, namespace: str) -> list[str]:
    """Get owner @usernames for (cluster, namespace) from CODEOWNERS. Returns list of @user."""
    if not codeowners_dir or not codeowners_dir.is_dir():
        return []
    pattern = f"/tenants-config/cluster/{cluster}/"
    first_matching_line: str | None = None
    for fname in ("CODEOWNERS", "staging/CODEOWNERS"):
        path = codeowners_dir / fname
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("#"):
                continue
            if pattern in line_stripped and namespace in line_stripped:
                first_matching_line = line_stripped
                break
        if first_matching_line is not None:
            break
    if first_matching_line is None:
        return []
    owners = [p for p in first_matching_line.split() if p.startswith("@")]
    return sorted(set(owners))


def _get_user_display(username: str) -> str:
    """Get 'Name <email>' for a GitLab username via glab. Returns display string."""
    if not username:
        return "(unknown)"
    try:
        result = subprocess.run(
            ["glab", "api", f"users?username={username}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout:
            return f"@{username} (lookup failed)"
        data = json.loads(result.stdout)
        if isinstance(data, list) and data:
            data = data[0]
        if not data:
            return f"@{username} (lookup failed)"
        name = (data.get("name") or "").strip()
        if not name:
            return f"@{username} (lookup failed)"
        email = data.get("public_email")
        if email and str(email) != "None":
            return f"{name} <{email}>"
        return f"{name} (no public email)"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return f"@{username} (lookup failed)"


def print_per_pod_summary(
    current_run_rows: list[dict[str, str]],
    run_date_str: str,
    output_dir: Path | None = None,
    codeowners_dir: Path | None = None,
) -> None:
    """
    Print a per-pod historical summary (same format as oom_logs_and_desc_bundle_generator).
    Uses base pod names (e.g. tekton-results-api-debug) so multiple instances are collated.
    Reports only for pods found in the current run; aggregates current run + historical
    from timestamped CSVs in output_dir when provided.
    """
    # Ensure each current run row has date
    for row in current_run_rows:
        if "date" not in row:
            row["date"] = run_date_str

    all_rows = list(current_run_rows)
    if output_dir and output_dir.is_dir():
        historical = _load_historical_rows_from_output_dir(output_dir)
        all_rows = current_run_rows + historical

    if not current_run_rows:
        return

    # Base names from current run only (report only for pods found this run)
    base_names = set(_pod_base_name(row["pod"]) for row in current_run_rows)
    if not base_names:
        return

    for base_name in sorted(base_names):
        # Match rows by computed base (same base normalizes to same display name)
        matching = [r for r in all_rows if _pod_base_name(r["pod"]) == base_name]
        if not matching:
            continue

        # Aggregate by (type, date, cluster, namespace) -> count
        agg: dict[tuple[str, str, str, str], int] = defaultdict(int)
        for row in matching:
            t = (row.get("type") or "").strip() or "OOMKilled"
            if t not in ("OOMKilled", "CrashLoopBackOff"):
                continue
            key = (t, row["date"], row["cluster"], row["namespace"])
            agg[key] += 1

        print()
        print("==============================================")
        print(f"Report for pod: {base_name}")
        print("==============================================")

        for event_type in ("OOMKilled", "CrashLoopBackOff"):
            keys_for_type = [(t, d, c, ns) for (t, d, c, ns) in agg if t == event_type]
            if not keys_for_type:
                print(f"{event_type}: 0 instances (no occurrences in date-wise CSVs)")
                continue
            # Sort by date, then cluster, then namespace
            for _, date_key, cluster, namespace in sorted(
                keys_for_type, key=lambda x: (x[1], x[2], x[3])
            ):
                count = agg[(event_type, date_key, cluster, namespace)]
                if codeowners_dir:
                    owners = _get_owners_for_namespace(codeowners_dir, cluster, namespace)
                    if owners:
                        displays = [_get_user_display(u.lstrip("@")) for u in owners]
                        owner_str = ", ".join(displays)
                        owner_part = f' is owned by "{owner_str}"'
                    else:
                        owner_part = " (no owner in CODEOWNERS)"
                else:
                    owner_part = " (no CODEOWNERS repo available)"
                print(
                    f"{event_type}: {count} instance(s) on {date_key}, "
                    f"Namespace: {namespace} (cluster: {cluster}){owner_part}"
                )
        print("==============================================")
