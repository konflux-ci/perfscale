# OOMKilled / CrashLoopBackOff detector (oc_get_ooms.py)

A high-performance, parallel OOMKilled / CrashLoopBackOff detector for OpenShift & Kubernetes clusters using `oc`, with rich exports and forensic artifact collection.

---

## 🚀 What This Tool Does

- Scans **one or many OpenShift clusters** (`oc` contexts)
- Detects:
  - **OOMKilled pods** (via events and pod status)
  - **CrashLoopBackOff pods** (via events and pod status)
- Configurable **time range filtering** (default: 1 day)
  - Format: `1h`, `6h`, `1d`, `7d`, `1M` (30 days), etc.
  - Events and **pod-status findings** (OOMKilled/CrashLoopBackOff) are filtered by this range; when a termination timestamp (e.g. `finishedAt`) exists, only findings within the window are included.
- Uses multiple detection methods:
  - Kubernetes **events** (optimized: single API call per namespace)
  - **Pod status** (direct check for OOMKilled/CrashLoopBackOff)
- Runs **highly parallel** with constant parallelism:
  - Cluster-level parallelism (maintains constant workers)
  - Namespace-level batching
  - Automatic load balancing across clusters
- Saves **forensic artifacts**:
  - `oc describe pod`
  - One log file with `oc logs --previous` (crashed run) then `oc logs` (current run)
- Exports **multiple formats** with absolute paths to artifacts and time range metadata:
  - **CSV** - Spreadsheet-friendly format (includes **Application** and **Component** from pod labels)
  - **JSON** - Structured automation input
  - **HTML** - Standalone visual report (open in browser)
  - **TABLE** - Human-readable text table
- Enriches each finding with **Application** and **Component** from pod `metadata.labels` (e.g. `appstudio.openshift.io/application`, `tekton.dev/pipelineTask`) so you can see which app/component a pod belongs to—no extra API calls.
- At the **end of each run**, prints a **per-pod summary** (same format as `oom_logs_and_desc_bundle_generator`): for each pod that had OOMKilled or CrashLoopBackOff in this run, one "Report for pod: …" block with instance counts per (type, cluster, namespace). Optional `-c` / `--codeowners-dir` shows namespace owner (name + email via `glab`). Then **generates per-pod tarballs** for each reported pod (use `--no-tarballs` to skip).
- Colorized terminal output

---

## 🧠 Architecture Overview

```text
                         ┌────────────────────────┐
                         │  oc config get-contexts│
                         └────────────┬───────────┘
                                      │
                    Constant Parallelism Pool (N workers)
                    (When one finishes, next starts immediately)
                                      │
          ┌───────────────────────────┴─────────────────────────────┐
          │                                                         │
┌─────────▼─────────┐                                     ┌─────────▼─────────┐
│  Cluster Worker   │                                     │  Cluster Worker   │
│ (context A)       │                                     │ (context B)       │
│                   │                                     │                   │
│  [Processing...]  │                                     │  [Processing...]  │
│                   │                                     │                   │
└─────────┬─────────┘                                     └─────────┬─────────┘
          │                                                         │
          │ When A finishes → Start Cluster C                       │
          │ When B finishes → Start Cluster D                       │
          │ (Maintains constant parallelism)                        │
          │                                                         │
          │ Fetch namespaces (with time range filter)               │
          │                                                         │
          │ Namespace batching (10 default)                         │
          │                                                         │
┌─────────▼─────────┐                                     ┌─────────▼─────────┐
│ Namespace Workers │  (parallel)                         │ Namespace Workers │
│  ┌──────────────┐ │                                     │  ┌──────────────┐ │
│  │Single API    │ │                                     │  │Single API    │ │
│  │call: events  │ │                                     │  │call: events  │ │
│  └──────────────┘ │                                     │  └──────────────┘ │
│  ┌──────────────┐ │                                     │  ┌──────────────┐ │
│  │Pod status    │ │                                     │  │Pod status    │ │
│  │check: OOM/CLB│ │                                     │  │check: OOM/CLB│ │
│  └──────────────┘ │                                     │  └──────────────┘ │
└─────────┬─────────┘                                     └─────────┬─────────┘
          │                                                         │
 Save artifacts:                                              Save artifacts:
 - pod describe                                               - pod describe
 - pod logs / previous                                        - pod logs / previous
          │                                                         │
┌─────────▼─────────┐                                     ┌─────────▼─────────┐
│ Multi-Format Export│                                     │ Multi-Format Export│
│ CSV/JSON/HTML/TABLE│                                     │ CSV/JSON/HTML/TABLE│
│ (with time_range)  │                                     │ (with time_range)  │
└───────────────────┘                                     └───────────────────┘
```

---

## ⚙️ Parallelism Model

| Layer            | Default | Controlled By | Notes |
|------------------|---------|---------------|-------|
| Cluster parallelism | 2       | `--batch` | **Constant parallelism**: When one cluster finishes, immediately starts the next one |
| Namespace batch  | 10      | `--ns-batch-size` | Number of namespaces processed per batch |
| Namespace workers| 5       | `--ns-workers` | Thread pool size for namespace processing |

**Key Improvements:**
- **Constant Parallelism**: Cluster processing maintains `--batch N` workers throughout execution. When one cluster completes, the next one starts immediately (no waiting for entire batch).
- **Optimized Events**: Single API call per namespace fetches all events, then filters in-memory (3x faster than previous approach).
- **Multiple Detection Methods**: Checks events and pod status for comprehensive coverage.

---

## 📂 Artifact Storage Layout

Artifacts are stored **per cluster** under the output directory so behavior is consistent on Mac and CI (e.g. Jenkins):

```
output/logs_and_description_files/<cluster_name>/
  <namespace>__<pod>__<timestamp>__desc.txt
  <namespace>__<pod>__<timestamp>__log.txt
```

Example:

```
output/logs_and_description_files/kflux-prd-es01/
  clusters-a53fda0e...__catalog-operator__2025-12-12T05-25-40Z__desc.txt
  clusters-a53fda0e...__catalog-operator__2025-12-12T05-25-40Z__log.txt
```

**One-time migration:** If you have existing artifacts under `/private/tmp/<cluster_name>/` (or `/tmp/<cluster_name>/`), run the migration script once to move them and repair paths in existing CSVs and HTMLs:

```bash
python migrate_artifacts_from_private_tmp.py [--output-dir output] [--dry-run]
```

The tool saves **both** log sources into one file (so you get logs from the crashed run and the current run):
1. **Previous container logs** (`oc logs <pod> --previous`) — from the container that OOM'd or crashed
2. **Current container logs** (`oc logs <pod>`) — from the current (possibly restarted) container

The log file contains two sections, clearly labeled:
```
=== Previous container logs (oc logs <pod> --previous) ===
...
=== Current container logs (oc logs <pod>) ===
...
```

---

## 📤 Output Formats

The tool automatically generates **four output formats** for maximum flexibility:

### 1. CSV (`oom_results.csv`)

Spreadsheet-friendly format with all findings in tabular form.

**Columns:**
```
cluster,
namespace,
pod,
type,
application,
component,
timestamps,
sources,
description_file,
pod_log_file,
time_range
```

**Application & Component:** Extracted from pod `metadata.labels` (no extra API calls). Used to identify which app and component a finding belongs to (e.g. Konflux/Tekton pipelines).
- **Application:** `appstudio.openshift.io/application` (e.g. `acs`), then `app.kubernetes.io/part-of`, `app.kubernetes.io/name`, or `app`.
- **Component:** `tekton.dev/pipelineTask` (e.g. `prefetch-dependencies`, `clone-repository`), then `tekton.dev/task`, `app.kubernetes.io/component`, or `component`. Empty if the pod has none of these labels.

**Type values:**
- `OOMKilled` - Pod was killed due to out-of-memory
- `CrashLoopBackOff` - Pod is in crash loop state

**Sources:**
- `events` - Found via Kubernetes events
- `oc_get_pods` - Found via direct pod status check

**Time Range:**
- Shows the time range used for detection (e.g., `1d`, `6h`, `1M`)

### 2. JSON (`oom_results.json`)

Structured format perfect for automation, scripting, and integration with other tools.

```json
{
  "_metadata": {
    "time_range": "1d"
  },
  "kflux-prd-es01": {
    "clusters-a53fda0e...": {
      "catalog-operator-79c5668759-hfrq8": {
        "pod": "catalog-operator-79c5668759-hfrq8",
        "oom_timestamps": [],
        "crash_timestamps": [
          "2025-12-12T05:25:40Z"
        ],
        "sources": ["events"],
        "application": "acs",
        "component": "prefetch-dependencies",
        "description_file": ".../output/logs_and_description_files/kflux-prd-es01/...__desc.txt",
        "pod_log_file": ".../output/logs_and_description_files/kflux-prd-es01/...__log.txt"
      }
    }
  }
}
```

**Structure:**
- `_metadata.time_range` - Time range used for detection
- `cluster` → `namespace` → `pod` → pod details
- `oom_timestamps` - Array of OOMKilled event timestamps
- `crash_timestamps` - Array of CrashLoopBackOff event timestamps
- `sources` - Array of detection methods used
- `application` - From pod labels (e.g. `appstudio.openshift.io/application`); empty if not set
- `component` - From pod labels (e.g. `tekton.dev/pipelineTask`); empty if not set

### 3. HTML (`oom_results.html`)

**Standalone visual report** that can be opened directly in any web browser (no server required; use `file://` or double-click). Perfect for sharing findings, quick visual overview, and presentation-ready reports.

**Features:**
- **Report header** – Performance and Scale Engineering; report title with generation timestamp (EST).
- **Historical trend graphs (all Konflux clusters)** – Two separate line charts:
  - **OOM** – Count of OOMKilled per run over time (red).
  - **CrashLoopBackOffs** – Count of CrashLoopBackOff per run over time (blue).
  - Dates on the X-axis (vertical labels), value labels above each point, horizontal scroll when there are many runs. Plot range is configurable (default 2 months) via `--plot-range` (e.g. `2M`, `7d`).
- **Table of total OOMs & CrashLoopBackOffs** – Historical trend table under the charts (Date (run), OOMKilled, CrashLoopBackOff) for the same plot range.
- **Per-cluster historical trend charts** – One combined chart per cluster (OOM + CrashLoopBackOff in the same graph), ordered by total occurrences (highest first). Heading format: *OOM & CrashLoopBackOffs - Historical trend (cluster: &lt;name&gt;) — Plot range: …*
- **Clusterwise Summary** – Table of findings by cluster (OOMKilled, CrashLoopBackOff, Total) with report timestamp in the section header.
- **PODs, Namespaces & Clusters Detailed Findings** – Sortable table with cluster, namespace, pod, type, **Application**, **Component**, timestamps, sources, Description File, Pod Log File, time range. Application and Component are taken from pod labels (e.g. Konflux/Tekton). Section header includes report timestamp. Table is horizontally scrollable so all columns (including log/description links) remain visible.
- **Historical HTML reports** – Table at the end listing past timestamped HTML reports; each row has a date (run) and an “Open report” link so you can open any previous run’s HTML from the current page.
- **Color-coded badges** – OOMKilled (red), CrashLoopBackOff (orange).
- **Clickable artifact links** – Description File and Pod Log File columns link to `file://` paths when present.
- **No external dependencies** – Fully self-contained HTML (inline SVG charts, no CDN).

The same HTML report is produced whether you run a full cluster scan or regenerate from existing data with `--print-summary-from-dir output` (see below). Simply **double-click** `oom_results.html` or open it via `file://` in your browser.

### 4. TABLE (`oom_results.table`)

Human-readable text table format, perfect for terminal viewing or plain text reports. Uses the same columns as the CSV (including Application and Component).

### 5. Per-pod summary (terminal, at end of run)

After writing the CSV/JSON/HTML/TABLE files, the tool prints a **per-pod summary** in the same format as `oom_logs_and_desc_bundle_generator`. For each pod that had at least one OOMKilled or CrashLoopBackOff in **this run**, you get a block like:

```
==============================================
Report for pod: kube-rbac-proxy-crio-ip-10-29-64-78.ec2.internal
==============================================
OOMKilled: 0 instances (no occurrences in this run)
CrashLoopBackOff: 1 instance(s) on 03-Feb-2026, Namespace: openshift-machine-config-operator (cluster: stone-stage-p01) (no owner in CODEOWNERS)
==============================================
```

- **Per-pod tarballs:** After the per-pod summary, `oc_get_ooms.py` automatically runs `oom_logs_and_desc_bundle_generator` for each reported pod (using a match string derived from actual pod names so the bundle generator finds CSV rows). Tarballs are written under `output/tarballs/`. Use **`--no-tarballs`** to skip this step.
- To show **namespace owner** (name + email from CODEOWNERS + GitLab) in each line, pass **`-c` / `--codeowners-dir`** with the path to konflux-release-data (directory containing `CODEOWNERS` and `staging/CODEOWNERS`). Requires `glab` to be installed and logged in.

```bash
./oc_get_ooms.py --time-range 1d -c /path/to/konflux-release-data
```

---

## 🧪 Example Runs

### Getting Help

To see all available options and usage information:

```bash
./oc_get_ooms.py --help
# or
./oc_get_ooms.py -h
```

### Basic Usage

#### Run on current context only
```bash
./oc_get_ooms.py --current
```

#### Run on all contexts (default)
```bash
./oc_get_ooms.py
```

#### Run on specific contexts using substrings
You can use **substrings** to match contexts (the script will automatically find the full context names):

```bash
./oc_get_ooms.py --contexts kflux-prd-rh02,stone-prd-rh01
```

The script will:
- Retrieve all available contexts from `oc config get-contexts` (or `kubectl` as fallback)
- Match each substring against available contexts (case-insensitive)
- Show confirmation messages: `Matched 'kflux-prd-rh02' -> 'default/api-stone-prod-p02-hjvn-p1-openshiftapps-com:6443/smodak'`
- Exit with an error if a substring matches multiple contexts or no contexts (with helpful suggestions)

**Note:** You can still use full context strings if preferred, but substrings are much more convenient.

### Performance Tuning

#### High-performance mode for very large clusters
```bash
./oc_get_ooms.py \
  --batch 4 \
  --ns-batch-size 250 \
  --ns-workers 250 \
  --timeout 200
```

**Note:** `--batch` maintains constant parallelism. With `--batch 4`, the tool always processes 4 clusters simultaneously. When one finishes, the next one starts immediately.

#### Moderate parallelism for medium clusters
```bash
./oc_get_ooms.py \
  --batch 2 \
  --ns-batch-size 50 \
  --ns-workers 20 \
  --timeout 60
```

### Time Range Filtering

Filter events by time range (default: 1 day):

```bash
# Last 30 seconds (for testing)
./oc_get_ooms.py --time-range 30s

# Last 1 hour
./oc_get_ooms.py --time-range 1h

# Last 6 hours
./oc_get_ooms.py --time-range 6h

# Last 1 day (default)
./oc_get_ooms.py --time-range 1d

# Last 7 days
./oc_get_ooms.py --time-range 7d

# Last 1 month (30 days)
./oc_get_ooms.py --time-range 1M
```

**Time range formats:**
- `s` = seconds (e.g., `30s`, `120s`)
- `m` = minutes (e.g., `15m`, `30m`)
- `h` = hours (e.g., `1h`, `6h`, `24h`)
- `d` = days (e.g., `1d`, `7d`, `30d`)
- `M` = months (30 days, e.g., `1M`, `2M`)

**Plot range for HTML historical graphs:** The HTML report’s trend charts show runs within a time window (default 2 months). Set it with `--plot-range` (same format as `--time-range`), e.g. `--plot-range 2M` or `--plot-range 7d`. This applies to both the full run and `--print-summary-from-dir`.

### Output directory

By default, all generated files (CSV, JSON, HTML, TABLE, and `logs_and_description_files/`) are written under the `output/` directory. Use `--output DIR` to use a different directory:

```bash
./oc_get_ooms.py --output /path/to/reports
./oc_get_ooms.py --current --output my-oom-run
```

### Namespace Filtering

#### Include only specific namespaces
```bash
# Only namespaces containing "tenant"
./oc_get_ooms.py --include-ns tenant

# Multiple patterns (OR logic)
./oc_get_ooms.py --include-ns "tenant|prod"
```

#### Exclude specific namespaces
```bash
# Exclude test namespaces
./oc_get_ooms.py --exclude-ns test

# Exclude multiple patterns
./oc_get_ooms.py --exclude-ns "debug|sandbox|test"
```

#### Combine include and exclude
```bash
# Include tenant namespaces, but exclude test ones
./oc_get_ooms.py \
  --include-ns tenant \
  --exclude-ns test

# Complex filtering
./oc_get_ooms.py \
  --include-ns "tenant|prod" \
  --exclude-ns "debug|sandbox|test"
```

### Combining Options

#### Production cluster scan with custom settings
```bash
./oc_get_ooms.py \
  --contexts prod-cluster \
  --time-range 1d \
  --include-ns "tenant|prod" \
  --exclude-ns "test|debug" \
  --batch 4 \
  --ns-batch-size 100 \
  --ns-workers 50 \
  --timeout 120 \
  --retries 5
```

#### Quick scan of current context (last 6 hours)
```bash
./oc_get_ooms.py \
  --current \
  --time-range 6h
```

#### Per-pod summary with CODEOWNERS owners
```bash
./oc_get_ooms.py --time-range 1d -c /path/to/konflux-release-data
```
The per-pod summary at the end will show namespace owner (name + email via `glab`) when available.

#### Comprehensive multi-cluster scan (last 7 days)
```bash
./oc_get_ooms.py \
  --contexts "prod,staging" \
  --time-range 7d \
  --batch 8 \
  --ns-batch-size 200 \
  --ns-workers 100
```

#### High-reliability scan with increased retries
```bash
./oc_get_ooms.py \
  --retries 5 \
  --timeout 300 \
  --time-range 1d
```

### Viewing Results

After running the tool, you'll find four output files:

```bash
# View CSV in spreadsheet
open oom_results.csv

# View JSON (pretty-printed)
cat oom_results.json | python -m json.tool

# View HTML report (double-click in Finder/File Explorer)
open oom_results.html

# View table format
cat oom_results.table
```

**Note:** If you ran the tool previously, your old files are automatically backed up with timestamps (e.g., `oom_results_13-Jan-2026_12-10-22-EDT.csv`). You can access historical data by opening the timestamped backup files.

#### Regenerating the HTML report without a cluster run

You can regenerate the full HTML report (including historical trend graphs and per-cluster charts) from existing CSV files in the output directory, without running against clusters:

```bash
./oc_get_ooms.py --print-summary-from-dir output
```

- Reads `oom_results.csv` as the “current run” and all `oom_results_*_*.csv` timestamped files for historical series and per-cluster data.
- Writes an updated `oom_results.html` with the same structure as when you run a full scan: trend graphs, Clusterwise Summary, PODs/Namespaces/Clusters Detailed Findings, and Historical HTML reports links.
- Requires no cluster access; useful for viewing trends after copying the `output/` directory elsewhere or when you only want to refresh the report from existing data.

You can pass a different directory, e.g. `--print-summary-from-dir /path/to/output`.

The **HTML report** (`oom_results.html`) is particularly useful for:
- Quick visual overview
- Sharing with team members
- Presentation-ready reports
- Clickable links to artifact files

---

## 🎨 Terminal Output

- **Green** → no issues
- **Yellow** → namespace scanned
- **Red** → OOM / CrashLoopBackOff detected
- **Cyan** → cluster boundaries
- **Gray** → skipped or unreachable clusters

---

## 🛡️ Resilience & Safety

- **Cluster connectivity check before run:** The tool checks connectivity to each cluster and prints a report (✓/✗ per cluster). It does **not** wait for user confirmation: if **at least one** cluster is connected, it proceeds with data collection; if **none** are connected, it aborts. No "Proceed with data collection? [y/N]:" prompt.
- Retries on TLS / API failures
- Configurable timeouts
- Graceful skipping of unreachable clusters
- **Time range filtering** to focus on recent events
- **Multiple detection methods** for comprehensive coverage:
  - Kubernetes events (optimized single API call)
  - Direct pod status checks (for currently existing pods)
- Namespaces printed **only if issues are found**

---

## 🔍 Why might I not see an OOM for a namespace?

If users report OOMs in a namespace (e.g. `preflight-dev-tenant`) but the tool reports none, common causes are:

1. **Different cluster or context**
   Reports may be from another cluster. You ran with `--contexts 'stone-stg-rh01'`; the OOMs might be on a different context (e.g. prod). Re-run for the context where OOMs were reported.

2. **Namespace not scanned (excluded)**
   The namespace might be excluded by:
   - **Ephemeral logic** (e.g. label `konflux-ci.dev/namespace-type: eaas`, or name patterns like `test-*`, `ci-*`, `clusters-<uuid>`). Use `--include-ephemeral` if that namespace is ephemeral but you still want it.
   - **Include/exclude patterns** (`--include-ns` / `--exclude-ns`). If you use `--include-ns`, the namespace must match one of the patterns.

3. **Pods no longer exist**
   OOM detection from **pod status** only sees **current** pods. If the OOMKilled pod was replaced or deleted, the new pod may not have `lastState.terminated.reason == OOMKilled` yet. Deleted pods are not visible to the tool.

4. **Events evicted or outside time range**
   **Events** are filtered by `--time-range` (e.g. `1d` = last 24 hours). Older events may have been evicted by the cluster (event TTL), or the OOM may have happened outside the window. Try a longer `--time-range` (e.g. `7d` or `1M`).

**How detection works (short):**
- **Events**: OOM/CrashLoop events in the namespace, filtered by `--time-range`. Older events can be evicted by the API server.
- **Pod status**: Current pods only; checks `state.terminated` and `lastState.terminated` for OOMKilled/CrashLoopBackOff. No history for pods that no longer exist.

**Verify that a namespace is scanned:**
- **List namespaces** that would be scanned (no cluster scan):
  ```bash
  ./oc_get_ooms.py --contexts 'stone-stg-rh01' --list-namespaces | grep preflight
  ```
- **Verbose run** to see which namespaces are skipped (ephemeral/include/exclude) and which are scanned:
  ```bash
  ./oc_get_ooms.py --contexts 'stone-stg-rh01' --verbose --time-range 1d
  ```

---

## 📌 Requirements

- Python **3.9+**
- `oc` CLI in PATH
- Logged in (`oc whoami` must succeed)

---

## 📄 Files Generated

| File | Purpose | Format |
|------|---------|--------|
| `oom_results.csv` | Spreadsheet-friendly output | CSV |
| `oom_results.json` | Structured automation input | JSON |
| `oom_results.html` | Visual report (open in browser) | HTML |
| `oom_results.table` | Human-readable text table | Plain text |
| `output/logs_and_description_files/<cluster>/*.txt` | Pod forensic artifacts | Text files |

**Note:** All output files are written to the **output directory** (default: `output/`). You can change it with `--output DIR` (e.g. `./oc_get_ooms.py --output /path/to/reports`). The HTML file is fully self-contained and can be opened directly in any web browser by double-clicking it.

### Automatic File Backup

The tool automatically backs up existing output files before generating new ones. If any of the output files (`oom_results.csv`, `oom_results.json`, `oom_results.html`, `oom_results.table`) already exist, they are automatically renamed with a timestamp before new files are created.

**Backup Format:**
```
oom_results_<DD-MMM-YYYY_HH-MM-SS-TZ>.csv
oom_results_<DD-MMM-YYYY_HH-MM-SS-TZ>.json
oom_results_<DD-MMM-YYYY_HH-MM-SS-TZ>.html
oom_results_<DD-MMM-YYYY_HH-MM-SS-TZ>.table
```

**Example:**
- Existing file: `oom_results.csv`
- Backed up as: `oom_results_13-Jan-2026_12-10-22-EDT.csv`
- New file created: `oom_results.csv`

This ensures you can:
- Keep historical data from previous runs
- Always have the latest results in standard filenames
- Compare results across different time periods

The tool will display which files were backed up when you run it:
```
Backed up 4 existing file(s):
  → oom_results_13-Jan-2026_12-10-22-EDT.csv
  → oom_results_13-Jan-2026_12-10-22-EDT.json
  → oom_results_13-Jan-2026_12-10-22-EDT.html
  → oom_results_13-Jan-2026_12-10-22-EDT.table
```

### Per-pod summary from `oc_get_ooms.py` vs bundle generator

- **`oc_get_ooms.py`** prints a per-pod summary **at the end of every run** (one "Report for pod: …" block per pod that had OOM/CrashLoop in that run), then **generates tarballs** for each of those pods by invoking `oom_logs_and_desc_bundle_generator` with a **match string** (longest common prefix of actual pod names) so the bundle generator finds the right rows in the CSV. Use `--no-tarballs` to skip tarball generation.
- **`oom_logs_and_desc_bundle_generator`** is used when you want **date-wise** reports and **tarballs** for a **specific pod** (e.g. run manually for one pod): it scans all date-wise CSVs in `output/` and builds one tarball per (type, date) for that pod. It matches the CSV pod column by **substring** (pass a literal that appears in the pod name, not the display name with `*`).

### Date-wise bundle generator (`oom_logs_and_desc_bundle_generator`)

The script `oom_logs_and_desc_bundle_generator` builds **date-specific** log/description tarballs for a **single pod** you pass on the command line. It uses the **date-wise CSV files** in `output/` (e.g. `oom_results_28-Jan-2026_14-55-05-EDT.csv`). It expects CSVs produced by the current `oc_get_ooms.py` (with application and component columns); tarballs are written under `output/tarballs/`.

**Usage:**
```bash
./oom_logs_and_desc_bundle_generator -p POD_NAME [ -d output ] [ -c /path/to/konflux-release-data ]
```

**Options:**
- **`-p` / `--pod-name`** (required): Pod name or substring to match (e.g. `oom-stress-retry` matches `oom-stress-retry-mp275`).
- **`-d` / `--output-dir`**: Directory containing date-wise CSVs (default: `output`). If it starts with `http://` or `https://`, it is treated as a **URL**: the script downloads the directory (by following HTML index listings), runs the generator in a temp dir, then uploads new tarballs to `<URL>/tarballs/`. **Auth:** set `REMOTE_TOKEN` only (Cookie auth, e.g. Jenkins session), or `REMOTE_USER` and `REMOTE_TOKEN`, or `JENKINS_USER` and `JENKINS_TOKEN`, or use `~/.netrc`. The server must expose directory listings and support PUT for uploads (e.g. Jenkins workspace). When using a URL, download/upload details are written to **`tarball_generation.log`** in the current directory; stdout shows a single-line progress. Parallel downloads use **20** workers by default (edit `DOWNLOAD_PARALLEL_JOBS` in the script to change).
- **`-c` / `--codeowners-dir`**: Path to konflux-release-data (contains `CODEOWNERS`, `staging/CODEOWNERS`). If **not** set, the script clones `git@gitlab.cee.redhat.com:releng/konflux-release-data.git` to a temp dir, uses it for owner lookup, then deletes it.

**Behavior:**
- **Local `-d`:** uses the given directory as-is. **URL `-d`:** downloads the directory (HTML index listing) into a temp dir, runs the steps below, then uploads new tarballs to `<URL>/tarballs/` (auth via env or .netrc).
1. Scans all date-wise CSVs (and `oom_results.csv`) in the output directory.
2. **Report**: For each (type, date, cluster, namespace), prints one line with instance count, **namespace**, **cluster**, and **owner name + email** (from CODEOWNERS + GitLab via `glab`). If `-c` is not passed, the script clones konflux-release-data to a temp dir for this lookup.
3. Creates **one tarball per (type, date)** with the **pod name in the filename**, under `output/tarballs/`, e.g.:
   - `output/tarballs/OOMKilled-oom-stress-retry-instance-28th-Jan-2026.tgz`
   - `output/tarballs/CrashLoopBackOff-<pod>-instance-29th-Jan-2026.tgz`
   Find all tarballs for a pod: `ls output/tarballs/*<pod-name>*.tgz`. Each tarball contains the description and log files for that pod on that date.

**Example report output:**
```
==============================================
Report for pod: oom-stress-retry
==============================================
OOMKilled: 1 instance(s) on 26-Jan-2026, Namespace: falrayes-tenant (cluster: stone-stg-rh01) is owned by "Faisal Al-Rayes <falrayes@redhat.com>"
OOMKilled: 3 instance(s) on 28-Jan-2026, Namespace: falrayes-tenant (cluster: stone-stg-rh01) is owned by "Faisal Al-Rayes <falrayes@redhat.com>"
CrashLoopBackOff: 0 instances (no occurrences in date-wise CSVs)
==============================================
```

**Requirements for owner name + email:** `git` (and SSH access to gitlab.cee.redhat.com) and `glab` (logged in). If clone or glab fails, the report still runs; owner part is omitted or shows "(no CODEOWNERS repo available)".

**Remote (URL) mode — short summary:**
- **Auth:** `REMOTE_TOKEN` only (Cookie) is enough for many Jenkins setups; no `REMOTE_USER` required. Or use `REMOTE_USER`+`REMOTE_TOKEN` or `JENKINS_USER`+`JENKINS_TOKEN`.
- **Log:** Download/upload details go to `tarball_generation.log` in the current directory; stdout shows a single-line progress (e.g. `Progress: 480/480 files (done)`).
- **Parallel downloads:** Default 20 workers; edit `DOWNLOAD_PARALLEL_JOBS` in the script to change.
- **Path handling:** The script normalizes Jenkins-style hrefs (base path stripped) to avoid duplicate-path warnings, and rewrites CSV artifact paths to local paths so tarballs are built from downloaded files.
- **Upload:** Uses HTTP PUT. If the server returns **405 Method Not Allowed**, uploads will fail (server does not allow PUT); the rest of the run (download, report, tarball creation) still succeeds.

**Examples:**
```bash
./oom_logs_and_desc_bundle_generator -p oom-stress-retry
./oom_logs_and_desc_bundle_generator -p loki-ingester-zone-a-0 -d output
./oom_logs_and_desc_bundle_generator -p image-controller-image-pruner-cronjob -c /path/to/konflux-release-data
# URL mode (e.g. Jenkins artifact URL): download → generate → upload tarballs to URL/tarballs/
# REMOTE_TOKEN only (Cookie) is enough for many Jenkins setups:
REMOTE_TOKEN='your-cookie-or-token' ./oom_logs_and_desc_bundle_generator -p my-pod -d https://jenkins.example.com/job/oom-reports/ws/artifacts
# Or: export REMOTE_USER=myuser REMOTE_TOKEN=mytoken
```

---

## 🧠 Design Philosophy

> **Fast, safe, forensic-grade, and cluster-scale.**

**Recent Enhancements (including Feb 12, 2026):**
- **Artifact path:** Pod logs and description files are saved under `output/logs_and_description_files/<cluster>/` (same on Mac and Jenkins). One-time migration from `/private/tmp/` is available via `migrate_artifacts_from_private_tmp.py`.
- **Cluster connectivity:** No user confirmation prompt. The tool checks connectivity to all clusters, prints a report, then proceeds automatically if at least one cluster is connected, or aborts if none are.
- **Constant Parallelism**: Maintains optimal resource utilization across all clusters.
- **Performance Optimized**: Single event API call per namespace (3x faster).
- **Comprehensive Detection**: Events + pod status checks ensure no OOM/CrashLoop pods are missed.
- **Time Range Filtering**: Focus on recent events with configurable lookback window.
- **Multi-Format Output**: CSV, JSON, HTML, and TABLE formats for maximum flexibility.
- **HTML Reports**: Standalone visual reports with clickable artifact links.
- **Consistent Output**: All formats are synchronized and include metadata.
- **Automatic per-pod tarballs:** After the per-pod summary, `oc_get_ooms.py` generates date-wise tarballs for each reported pod (via `oom_logs_and_desc_bundle_generator`), using a match string (longest common prefix of actual pod names) so the bundle generator finds CSV rows. Use `--no-tarballs` to skip.
- **Bundle generator (remote mode):** `REMOTE_TOKEN`-only Cookie auth; `tarball_generation.log` for details; single-line download progress; parallel downloads (`DOWNLOAD_PARALLEL_JOBS`); path normalization and CSV path rewriting for Jenkins-style URLs. Upload requires server to accept PUT (405 = not supported).

---

## 📝 License

Internal / Team Utility
Adapt as needed.

---

## 🔮 Future Enhancements & Roadmap

The following enhancements are **intentionally planned** and align with the current architecture.
Most can be added incrementally without redesigning the tool.

**Possible future: Logs from Splunk when cluster logs are gone.** When the tool runs on a 24h (or longer) interval, pod logs for older crashed containers may no longer be available from the cluster (`oc logs --previous` only reaches the most recent previous instance). Logs may be archived in Splunk (or, for TaskRuns, in kubearchive). A future option could be to fall back to fetching relevant logs from Splunk when cluster logs are missing. For Splunk API usage (including static tokens / service accounts for automation), see [Splunk API usage](https://source.redhat.com/departments/strategy_and_operations/it/splunk/splunk_wiki/splunk_api_usage). This is noted here as an idea for the future if the team decides to pursue it.

---

### 📈 1. OOM / CrashLoopBackOff Trend Analysis

Analyze **patterns over time** across:

- Namespaces
- Clusters
- Workloads
- Time windows

Examples:

- Which namespaces OOM most frequently?
- Which clusters are most unstable?
- Are OOMs increasing week-over-week?
- Which pods repeatedly crash after restarts?

#### Possible Outputs

```text
Cluster          Namespace        OOMs(7d)  CLB(7d)  Trend
----------------------------------------------------------
kflux-prd-es01   tenant-a         24        3        ↑↑
kflux-prd-es01   tenant-b         2         15       ↑
stone-prd-rh01   tenant-x         0         8        →
```

This can be implemented by:
- Persisting JSON outputs across runs
- Aggregating by `(cluster, namespace, pod)`
- Applying rolling time windows (7d / 30d)

---

### 📊 2. Namespace Stability Scoring

Compute a **stability score** per namespace:

```text
score = f(OOM count, CrashLoop count, restart frequency)
```

Example:

```text
Namespace        Score   Status
--------------------------------
tenant-prod-a    92      Stable
tenant-prod-b    61      Warning
tenant-prod-c    28      Critical
```

This allows:
- Ranking tenants
- Capacity planning
- SLO enforcement

---

### 🧠 3. Memory Pressure Correlation

Correlate OOMs with:
- Container memory limits
- Actual memory usage
- Node memory pressure

Answer questions like:
- Are OOMs caused by under-sized limits?
- Are multiple namespaces competing on the same nodes?
- Do OOMs align with traffic spikes?

---

### 📉 4. Historical Baseline & Regression Detection

Automatically detect regressions:

- “Namespace X normally has 0–1 OOMs/week, now has 12”
- “CrashLoopBackOff appeared after deployment Y”

This could integrate with:
- Deployment timestamps
- Image changes
- ConfigMap updates

---

### 🧾 5. Persistent Storage Backend

Optional persistence layer:

- SQLite (local)
- PostgreSQL
- S3 / Object Storage

Use cases:
- Long-term trend analysis
- Dashboards
- Audit trails

---

### 📊 6. Enhanced HTML Reports (✅ Implemented)

HTML report generation is implemented. The tool generates `oom_results.html` with:
- Summary statistics (Clusterwise Summary) and Detailed Findings (PODs, Namespaces & Clusters) with report timestamp in headers
- Historical trend line charts (all Konflux clusters): separate OOM and CrashLoopBackOff charts; per-cluster combined charts ordered by total occurrences
- Table of total OOMs & CrashLoopBackOffs and Historical HTML reports (links to past timestamped reports)
- Clickable artifact links (Description File, Pod Log File); horizontally scrollable details table
- Self-contained HTML (inline SVG, no external deps); works with `file://`

Possible future enhancements:
- Export to PDF
- Customizable themes

---

### 📡 7. Alerting & Integrations

Integrations could include:

- Slack
- Email
- PagerDuty
- Jira / ServiceNow
- GitHub Issues

Example:
```text
ALERT: tenant-prod-x had 5 OOMs in last 6h on cluster kflux-prd-es01
```

---

### 📍 8. Grafana Annotations

Automatically annotate Grafana dashboards when:
- OOMs occur
- CrashLoopBackOff starts
- Thresholds are exceeded

This links incidents directly to metrics timelines.

---

### 🧪 9. Canary / Deployment Awareness

Enhance detection by:
- Linking OOMs to recent rollouts
- Identifying bad canary deployments
- Comparing old vs new ReplicaSets

---

### 🧵 10. Event Deduplication & Root Cause Grouping

Group related failures:

- Same pod template
- Same container
- Same error signature

Example:

```text
Root Cause: insufficient memory limit (256Mi)
Affected Pods: 17
Affected Namespaces: 4
```

---

### 🔐 11. RBAC & Least-Privilege Mode

Add flags for:
- Namespace-scoped scanning
- Read-only operation
- Limited artifact collection

Useful for:
- Tenant self-service diagnostics
- Restricted environments

---

### 🧩 12. Plugin Architecture

Enable pluggable detectors:

```text
detectors/
  oom.py
  crashloop.py
  diskpressure.py
  cpuhog.py
```

Allow teams to add custom failure modes without modifying core logic.

---

## 🧠 Long-Term Vision

> Move from **reactive troubleshooting** → **predictive reliability insights**

This tool can evolve into:
- A fleet-wide reliability scanner
- A capacity planning assistant
- An SRE forensic toolkit

---


