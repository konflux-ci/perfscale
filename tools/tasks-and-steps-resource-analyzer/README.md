Tasks and Steps Resource Analyzer
===================================

Analyzes **actual** memory, CPU, and disk I/O usage of every step in a Konflux Tekton Task
across all clusters and recommends right-sized Kubernetes `computeResources`.
Data comes from Prometheus (`container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`,
`container_fs_reads/writes_bytes_total`) over a configurable window (default: last 7 days).

Supports MAX, P95, P90, and Median as recommendation bases, with a configurable safety margin.

---

## Quick Start

> ⏱ **Runtime**: 30 seconds to several hours depending on cluster data volume.
> Use `--pll-clusters 4 --pll-queries 4` (see [Speed Tips](#speed-tips)) for the fastest run.

**Step 1 — Clone and set up a Python virtual environment**

```bash
git clone git@github.com:redhat-appstudio/perfscale.git
cd perfscale
python -m venv venv && source venv/bin/activate
pip install requests pyyaml
```

**Step 2 — Log in to all Konflux clusters**

You need a valid `kubeconfig` for every cluster you want data from.
The easiest way is to use the `oclogin-all` helper in this repo:

```bash
# Install oclogin / oclogin-all into your PATH, then:
oclogin-all
```

> ⚠️ `oclogin-all` **deletes and recreates** kubeconfig entries for all 16 known Konflux clusters.
> Use with caution if you have other kubeconfig entries you want to preserve.
>
> Alternatively, log in to clusters manually so they appear in your `~/.kube/config`.

**Step 3 — Run the analyzer**

```bash
cd tools/tasks-and-steps-resource-analyzer

./analyze_resource_limits.py \
  --file https://github.com/konflux-ci/build-definitions/blob/main/task/fbc-fips-check-oci-ta/0.1/fbc-fips-check-oci-ta.yaml \
  --analyze-again --margin 5 --days 15 --pll-clusters 4 --pll-queries 4
```

Replace the `--file` URL with the task YAML you want to analyze. You will be shown a confirmation
prompt listing the task name and steps before any data is collected.

**Step 4 — Open the HTML reports**

All output files land in `.analyze_cache/`:

```bash
ls -l .analyze_cache/*fbc-fips-check-oci-ta*.html
```

Example output:
```
.analyze_cache/fbc-fips-check-oci-ta_analyzed_data_20260409.html
.analyze_cache/fbc-fips-check-oci-ta_comparison_data_margin-5_20260409.html
.analyze_cache/fbc-fips-check-oci-ta_analyzed_data_detailed_step_fips-operator-check-step-action_20260409.html
.analyze_cache/fbc-fips-check-oci-ta_analyzed_data_detailed_step_get-unique-related-images_20260409.html
...
```

Open the **`_comparison_data_`** HTML in your browser — it shows all four base metrics (MAX, P95,
P90, Median) side by side so you can pick the right one for your PR.

**Step 5 — Apply changes manually**

The tool **never modifies YAML files automatically**. Use the comparison report to decide which
base metric to use, then update your task YAML accordingly.

---

## Speed Tips

| Goal | Recommended flags |
|---|---|
| Fastest run | `--pll-clusters 4 --pll-queries 4` (clusters + queries in parallel) |
| Wider history | `--days 15` (15 days instead of default 7) |
| Force fresh data | `--analyze-again` (ignores existing cache) |
| Skip data collection | `--update` (reuse existing Phase 1 cache) |
| Validate before running | `--dry-run` (connectivity check only) |

**Recommended command for most analyses:**

```bash
./analyze_resource_limits.py \
  --file <github-or-local-yaml-url> \
  --analyze-again --margin 5 --days 15 --pll-clusters 4 --pll-queries 4
```

**Serial vs parallel:**
- Default (no `--pll-clusters`): clusters processed one at a time — safe but slow
- `--pll-clusters 4`: 4 clusters processed concurrently — ~4× faster, recommended
- More than 6 workers may hit Prometheus rate limits; 4 is a good default
- `--pll-queries N` (default 2, max 4): within each cluster worker, all 4 per-pod
  Prometheus queries (mem / cpu / io_read / io_write) are dispatched to a small thread
  pool of size N. `--pll-queries 4` issues all 4 queries simultaneously per pod.

---

## Two-Phase Workflow

### Phase 1 — Analysis (collect data, generate reports)

```bash
./analyze_resource_limits.py --file <yaml> --margin 5 --days 15 --pll-clusters 4
```

- Collects per-pod metrics from all clusters in parallel
- Computes MAX, P95, P90, Median for every step
- Saves to `.analyze_cache/`:
  - `{task}_analyzed_data_{date}.html/json` — aggregate table
  - `{task}_comparison_data_margin-5_{date}.html/json` — **main report**: all four bases, proposed vs current, plus violators sub-tables
  - `{task}_analyzed_data_detailed_step_{step}_{date}.html/json/csv` — one file per step, every pod execution

At the end, the tool prints the path to the comparison HTML and exits. **No YAML is modified.**

### Phase 2 — Update (view recommendations for a different margin, no re-collection)

```bash
./analyze_resource_limits.py --update --file <yaml> --margin 10
```

- Reuses Phase 1 cache, generates a new comparison file for the requested margin
- Creates `{task}_comparison_data_margin-10_{date}.html/json` alongside the existing `margin-5` file
- Useful for comparing conservative vs aggressive margins without re-running data collection

---

## Key Options

| Flag | Default | Description |
|---|---|---|
| `--file FILE` | — | Local YAML path or GitHub URL (required for Phase 1) |
| `--days N` | 7 | History window for data collection |
| `--margin N` | 5 | Safety margin % added to the base metric |
| `--pll-clusters N` | serial | Number of clusters to query in parallel |
| `--pll-queries N` | 2 | Per-pod query parallelism (mem/cpu/io_read/io_write), max 4 |
| `--analyze-again` / `--aa` | off | Force re-collection even if today's cache exists |
| `--update` | off | Phase 2: regenerate comparison from cache, no re-collection |
| `--dry-run` | off | Connectivity check only, no data collection |
| `--debug` | off | Verbose output for troubleshooting |

> **Note on `--base`:** This flag is accepted but **ignored** — Phase 1 always generates all four
> bases (max, p95, p90, median) so you can choose in the HTML report without re-running.

---

## Understanding the HTML Reports

### `_comparison_data_margin-N_` (the main decision report)

Open this first. For each base metric (MAX / P95 / P90 / Median) it shows:

- **Proposed Requests / Limits** vs current values from the YAML
- **Violators sub-table** (collapsible ▾): which namespaces, applications, and components had
  observed usage *above* that base threshold — organized by namespace → application → component,
  with exact peak values and `+delta ⚠` annotations. This answers *"who would OOM if I pick P95?"*
- **Heavy-tail warning** (amber banner): steps where Max/P95 ratio ≥ 3× — rare but heavy workloads
  can exceed the P95 line significantly
- **Cluster data coverage** (blue banner): how many days of actual data each cluster returned
  vs what was requested — reveals when Prometheus doesn't have the full window
- **Scrape interval notice**: reminder that Prometheus scrapes every 15–30 s; sub-scrape memory
  spikes (e.g. brief image decompression) may not be captured

### `_analyzed_data_detailed_step_{step}_` (per-pod drill-down)

Sortable table of every pod execution observed. Includes:
- Peak memory (MB) and CPU (cores) per pod
- **Peak Disk Read / Write (MB/s)** — rows highlighted amber when I/O exceeds 50 MB/s
- Namespace, application, component attribution
- Current requests and limits from the YAML
- Timestamp of the execution

Sort by `Final Memory Usage` or `Peak Disk Read` to quickly spot outlier workloads.

---

## Choosing a Base Metric

| Base | Coverage | When to use |
|---|---|---|
| **MAX** | 100% of observed runs | Critical tasks where any OOM is unacceptable |
| **P95** | 95% of runs | Most production tasks — balances safety and cost |
| **P90** | 90% of runs | Cost-optimized tasks where rare OOMs are tolerable |
| **Median** | 50% of runs | Non-critical / dev workloads only |

**Recommendation for Konflux production tasks:** Start with P95 + 5% margin. If the comparison
report shows significant violators for P95 (especially large or well-known tenants), consider MAX.

**Calculation:**
```
recommended = max_across_clusters(base_metric) × (1 + margin / 100)
# rounded UP to next 64Mi increment (< 1Gi) or whole Gi (≥ 1Gi) for memory
# rounded UP to next 50m for CPU, minimum 50m
```

---

## Output Files Reference

| Pattern | Phase | Content |
|---|---|---|
| `{task}_analyzed_data_{date}.html/json` | 1 | Aggregate table: per-cluster MAX/P95/P90/Median |
| `{task}_comparison_data_margin-{N}_{date}.html/json` | 1 & 2 | All four bases, proposed vs current, violators |
| `{task}_analyzed_data_detailed_step_{step}_{date}.html/json/csv` | 1 | Per-pod execution data with I/O |

All files land in `.analyze_cache/`. Re-running on the same date adds a `_HHMMSS` suffix to avoid
overwriting earlier runs.

---

## Architecture

```
analyze_resource_limits.py  (orchestrator)
  │
  ├── per-cluster worker (threaded, --pll-clusters N)
  │     ├── list_pods_for_a_particular_task.py   → Prometheus kube_pod_labels query
  │     └── query_prometheus_range.py            → per-pod memory / CPU / I/O range queries
  │           dispatched via inner ThreadPoolExecutor (--pll-queries N, default 2, max 4)
  │           adaptive step: 30s (≤1d) · 5m (≤7d) · 15m (≤30d) · 1h (>30d)
  │
  └── wrapper_for_promql_for_all_clusters.sh  (legacy aggregation path, batched per-cluster)
        └── wrapper_for_promql.sh  (per-cluster: 50-pod batches, max/p95/p90/median)
```

The primary path for `--file` usage is the Python threaded worker (direct per-pod queries).
The shell wrapper path is the legacy stdin/pipe path.

---

## Cluster Authentication

Log in to all clusters before running. Using the `oclogin` / `oclogin-all` helpers in this repo
is the fastest approach — they generate kubeconfig entries for all 16 Konflux clusters.

```bash
# After installing helpers into PATH:
oclogin-all      # logs into all 16 clusters at once
```

Alternatively, log into clusters individually using `oc login` and ensure all contexts are
present in your `~/.kube/config`.

---

## Known Limitations

- **Prometheus scrape interval (~15–30 s):** Sub-scrape memory spikes are not captured.
  `max_over_time` takes the max of all scraped samples — if a spike resolved between two scrapes,
  it is invisible. The comparison HTML includes a notice about this.
- **Prometheus history gaps:** Some clusters may return fewer days than requested if their
  Prometheus retention is shorter. The cluster coverage banner in the HTML reports this per cluster.
- **Bimodal distributions:** A step that handles both lightweight and very heavy payloads (e.g.
  RHOAI FBC bundles vs small operators) will show very different MAX vs P95 values. The heavy-tail
  warning banner flags these cases.

---

## Future Enhancement: Long-term Metrics via RHOBS

The tool is currently bounded by each cluster's local Prometheus retention (typically 4-11 days).
A longer-term data source exists but is not yet connected:

**RHOBS (Red Hat Observability Service)** retains metrics for up to a year and is queryable via
AppSRE Grafana (datasource: `rhtap-observatorium-stage` / `rhtap-observatorium-production`).

As noted by Faisal Al-Rayes in
[KONFLUX-13351](https://redhat.atlassian.net/browse/KONFLUX-13351?focusedCommentId=16869177):

- Raw per-pod metrics (`container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`)
  are **not** forwarded to RHOBS — the per-pod label combination is too high in cardinality for
  the shared Prometheus instance.
- An **aggregated form** (per `container/tektonstepname` per namespace, not per unique pod) **is**
  forwarded and retained long-term.
- Extending the aggregation to cover `*-tenant` namespaces is potentially feasible, but requires a
  cardinality and ephemeral-namespace review before enabling.

**Potential future work:** if `*-tenant` namespaces are added to the RHOBS aggregation rules, the
tool could query RHOBS as a long-term supplement — providing step-level trend data well beyond
Prometheus retention. Per-pod execution detail (needed for distribution analysis) would still
require local Prometheus.

---

## Contributing / Feedback

The tool has been used across Konflux to right-size resources for tasks including `buildah`,
`fbc-fips-check`, `prefetch-dependencies`, and many others. If you find a bug, notice data
that looks off, or have ideas for improvement:

- Open an issue or PR in [redhat-appstudio/perfscale](https://github.com/redhat-appstudio/perfscale)
- Share your findings in `#konflux-users` or `#forum-konflux-build`
- Run the tool on your own task and share the HTML report — more data from more tenants improves
  recommendations for everyone
