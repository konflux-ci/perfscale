"""Task YAML fetch/parse helpers."""

import re
import sys
from pathlib import Path
from typing import Any

try:
    import requests
    import urllib3
    import yaml
except ImportError:
    print(
        "Error: Missing required library. Install with: pip install requests pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def convert_github_url_to_raw(url) -> Any:
    """Convert GitHub blob URL to raw content URL."""
    # Convert blob URL to raw URL
    # https://github.com/user/repo/blob/branch/path -> https://raw.githubusercontent.com/user/repo/branch/path
    pattern = r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)"
    match = re.match(pattern, url)
    if match:
        user, repo, branch, path = match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
    return url


def fetch_yaml_content(file_path_or_url) -> Any:
    """Fetch YAML content from file path or URL."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        url = convert_github_url_to_raw(file_path_or_url)
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return yaml.safe_load(response.text), url
        except requests.RequestException as e:
            print(f"Error fetching URL {url}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        path = Path(file_path_or_url)
        if not path.exists():
            print(f"Error: File not found: {file_path_or_url}", file=sys.stderr)
            sys.exit(1)
        with open(path) as f:
            return yaml.safe_load(f), str(path.absolute())


def extract_task_info(yaml_content) -> Any:
    """Extract task name, step names, and current resource limits from Tekton Task YAML."""
    task_name = yaml_content.get("metadata", {}).get("name", "")
    steps = []
    current_resources = {}

    # Get default resources from stepTemplate
    # Support both Tekton v1 (computeResources) and v1beta1 (resources) field names
    step_template = yaml_content.get("spec", {}).get("stepTemplate", {})
    default_resources = step_template.get("computeResources") or step_template.get("resources", {})
    default_mem_req = default_resources.get("requests", {}).get("memory", "")
    default_cpu_req = default_resources.get("requests", {}).get("cpu", "")
    default_mem_lim = default_resources.get("limits", {}).get("memory", "")
    default_cpu_lim = default_resources.get("limits", {}).get("cpu", "")

    # Extract step names and current resources from spec.steps
    for step in yaml_content.get("spec", {}).get("steps", []):
        step_name = step.get("name", "")
        if step_name:
            steps.append(step_name)

            # Get current resources for this step (use defaults if not specified)
            # Support both Tekton v1 (computeResources) and v1beta1 (resources) field names
            step_resources = step.get("computeResources") or step.get("resources", {})
            step_req = step_resources.get("requests", {})
            step_lim = step_resources.get("limits", {})

            # Get values, using None if not set (to distinguish from empty string)
            mem_req = (
                step_req.get("memory")
                if "memory" in step_req
                else (default_mem_req if default_mem_req else None)
            )
            cpu_req = (
                step_req.get("cpu")
                if "cpu" in step_req
                else (default_cpu_req if default_cpu_req else None)
            )
            mem_lim = (
                step_lim.get("memory")
                if "memory" in step_lim
                else (default_mem_lim if default_mem_lim else None)
            )
            cpu_lim = (
                step_lim.get("cpu")
                if "cpu" in step_lim
                else (default_cpu_lim if default_cpu_lim else None)
            )

            current_resources[step_name] = {
                "requests": {
                    "memory": mem_req,
                    "cpu": cpu_req,
                },
                "limits": {
                    "memory": mem_lim,
                    "cpu": cpu_lim,
                },
            }

    return task_name, steps, default_resources, current_resources


def normalize_step_name_for_compare(name) -> Any:
    """Strip Tekton step- prefix so YAML names match CSV step column."""
    if not name:
        return ""
    s = str(name).strip()
    if s.startswith("step-"):
        return s[5:]
    return s


def compute_steps_missing_observability(declared_steps, by_step) -> Any:
    """Declared YAML steps that have no rows in aggregated observability data.

    Args:
        declared_steps: Iterable of step names as in YAML (with or without step- prefix)
        by_step: defaultdict or dict keyed by step name as in CSV (no step- prefix)

    Returns:
        Sorted list of step names (no step- prefix) missing from data.
    """
    declared = {
        normalize_step_name_for_compare(s)
        for s in (declared_steps or [])
        if normalize_step_name_for_compare(s)
    }
    seen = set(by_step.keys()) if by_step else set()
    return sorted(declared - seen)
