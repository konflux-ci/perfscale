"""Cluster context selection, connectivity, and confirmation prompts."""

import re
import subprocess
import sys

from .task_yaml import normalize_step_name_for_compare


def read_wrapper_config(wrapper_path):
    """Read TASK_NAME and STEPS from wrapper script.

    Returns:
        tuple: (task_name, steps_list, is_defined)
        - task_name: Task name if defined, None otherwise
        - steps_list: List of step names (without 'step-' prefix) if defined, None otherwise
        - is_defined: True if both TASK_NAME and STEPS are defined (not commented, non-empty)
    """
    task_name = None
    steps_list = None
    is_defined = False

    try:
        with open(wrapper_path) as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue

            # Check for TASK_NAME
            if stripped.startswith("TASK_NAME="):
                # Extract value between quotes
                match = re.search(r'TASK_NAME="([^"]*)"', line)
                if match:
                    task_name = match.group(1).strip()

            # Check for STEPS
            elif stripped.startswith("STEPS="):
                # Extract value between quotes
                match = re.search(r'STEPS="([^"]*)"', line)
                if match:
                    steps_str = match.group(1).strip()
                    if steps_str:
                        # Split by space and remove 'step-' prefix
                        steps_list = [normalize_step_name_for_compare(s) for s in steps_str.split()]

        # Both must be defined and non-empty
        is_defined = (
            task_name is not None
            and task_name != ""
            and steps_list is not None
            and len(steps_list) > 0
        )

    except Exception as e:
        print(f"Warning: Could not read wrapper script: {e}", file=sys.stderr)

    return task_name, steps_list, is_defined


def validate_wrapper_steps(wrapper_task, wrapper_steps, yaml_task, yaml_steps):
    """Validate wrapper-defined task and steps against YAML file.

    Args:
        wrapper_task: Task name from wrapper script
        wrapper_steps: List of step names from wrapper (without 'step-' prefix)
        yaml_task: Task name from YAML file
        yaml_steps: List of step names from YAML file

    Returns:
        tuple: (is_valid, error_messages)
        - is_valid: True if validation passes
        - error_messages: List of error messages (empty if valid)
    """
    errors = []

    # Check task name match (case-sensitive)
    if wrapper_task != yaml_task:
        errors.append(
            f"Task name mismatch: wrapper has '{wrapper_task}', YAML file has '{yaml_task}'"
        )

    # Convert to sets for comparison (normalize step names)
    wrapper_steps_set = set(wrapper_steps)
    yaml_steps_set = set(yaml_steps)

    # Check if wrapper steps are subset or equal to YAML steps
    extra_steps = wrapper_steps_set - yaml_steps_set
    if extra_steps:
        errors.append(f"Wrapper defines steps not found in YAML file: {sorted(extra_steps)}")

    missing_steps = yaml_steps_set - wrapper_steps_set
    if missing_steps:
        # This is a warning, not an error (wrapper can be a subset)
        pass

    is_valid = len(errors) == 0
    return is_valid, errors


_SA_CONTEXT_MARKER = "system:serviceaccount"
_KONFLUX_USER_CONTEXT_RE = re.compile(r"/api-(?:kflux|stone)-", re.IGNORECASE)
_OPENSHIFTAPPS_CLUSTER_RE = re.compile(
    r"/api-(.+?)-[a-z0-9]{4}-p[0-9]-openshiftapps",
    re.IGNORECASE,
)
_CONNECTIVITY_TIMEOUT_SEC = 15


def is_konflux_user_context(ctx):
    """Return True for a human oc/oclogin Konflux context, not SA or unrelated clusters."""
    if not ctx or _SA_CONTEXT_MARKER in ctx:
        return False
    return bool(_KONFLUX_USER_CONTEXT_RE.search(ctx))


def list_kubectl_context_names():
    result = subprocess.run(
        ["kubectl", "config", "get-contexts", "-o", "name"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return []
    return [c.strip() for c in result.stdout.splitlines() if c.strip()]


def _wrapper_contexts_assignment(wrapper_path):
    with open(wrapper_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("CONTEXTS="):
                match = re.search(r'CONTEXTS="([^"]*)"', line)
                if match:
                    return match.group(1).strip()
    return None


def resolve_wrapper_contexts(wrapper_path):
    """Resolve kubeconfig context names from the wrapper CONTEXTS= line.

    Returns:
        tuple: (contexts, error_message). error_message is None on success.
    """
    try:
        contexts_str = _wrapper_contexts_assignment(wrapper_path)
    except OSError as e:
        return None, f"Error reading wrapper script: {e}"

    if not contexts_str:
        contexts = list_kubectl_context_names()
        if not contexts:
            return None, "Could not get cluster contexts"
        return contexts, None

    if "$(" not in contexts_str:
        return [c.strip() for c in contexts_str.split() if c.strip()], None

    cmd_match = re.search(r"\$\(([^)]+)\)", contexts_str)
    if not cmd_match:
        return None, "Invalid CONTEXTS command substitution"
    cmd = cmd_match.group(1).strip()
    if "kubectl config get-contexts" not in cmd:
        return None, f"Unsupported CONTEXTS command: {cmd}"

    contexts = list_kubectl_context_names()
    if contexts:
        return contexts, None
    fallback_match = re.search(r'echo\s+[\'"]([^\'"]+)[\'"]', contexts_str)
    if fallback_match:
        return [fallback_match.group(1).strip()], None
    return None, "Could not execute CONTEXTS command"


def select_analyzer_contexts(contexts, announce_ignored=False):
    """Keep one Konflux user context per cluster; drop SA leftovers and other clusters."""
    konflux = []
    ignored = []
    for ctx in contexts:
        if not ctx:
            continue
        if is_konflux_user_context(ctx):
            konflux.append(ctx)
        else:
            ignored.append(ctx)

    selected_by_name = {}
    for ctx in konflux:
        name = get_cluster_display_name(ctx)
        prev = selected_by_name.get(name)
        if prev is None or (ctx.startswith("default/") and not prev.startswith("default/")):
            selected_by_name[name] = ctx

    order = {ctx: i for i, ctx in enumerate(konflux)}
    selected = sorted(selected_by_name.values(), key=lambda c: order.get(c, 10**9))

    if announce_ignored and ignored:
        print(
            f"Ignoring {len(ignored)} kubeconfig context(s) that are not Konflux user logins.",
            file=sys.stderr,
        )
    return selected


def check_cluster_connectivity(wrapper_path):
    """Check connectivity to Konflux clusters from the wrapper / kubeconfig.

    Probes with ``kubectl --context`` so the current kubeconfig context is left unchanged.
    Service-account leftovers, Lightwell (unless named like api-kflux/api-stone), and
    unrelated clusters such as dno-ocp-hub are skipped.

    Returns:
        tuple: (all_connected, connectivity_report)
        - all_connected: True if all selected clusters are accessible
        - connectivity_report: List of (cluster_display_name, status, error_message) tuples
    """
    report = []
    all_connected = True

    contexts, error = resolve_wrapper_contexts(wrapper_path)
    if error:
        return False, [("unknown", False, error)]

    contexts = select_analyzer_contexts(contexts, announce_ignored=True)
    if not contexts:
        return False, [("unknown", False, "No Konflux user kubeconfig contexts found")]

    timeout_flag = f"{_CONNECTIVITY_TIMEOUT_SEC}s"
    for ctx in contexts:
        display_name = get_cluster_display_name(ctx)
        try:
            test_result = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "namespaces",
                    "--context",
                    ctx,
                    f"--request-timeout={timeout_flag}",
                ],
                capture_output=True,
                text=True,
                timeout=_CONNECTIVITY_TIMEOUT_SEC + 5,
            )
            if test_result.returncode == 0:
                report.append((display_name, True, "Connected"))
            else:
                err = (test_result.stderr or test_result.stdout or "unknown error").strip()
                report.append((display_name, False, f"Cannot access cluster: {err[:100]}"))
                all_connected = False
        except subprocess.TimeoutExpired:
            report.append((display_name, False, "Connection timeout"))
            all_connected = False
        except Exception as e:
            report.append((display_name, False, f"Error: {str(e)[:100]}"))
            all_connected = False

    return all_connected, report


def prompt_confirmation(task_name, steps, source="extracted from YAML"):
    """Prompt user for confirmation before proceeding.

    Args:
        task_name: Task name to confirm
        steps: List of step names to confirm
        source: Source of the values (for display)

    Returns:
        bool: True if user confirms, False otherwise
    """
    print("\n" + "=" * 80, file=sys.stderr)
    print("CONFIRMATION: Task and Steps Configuration", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(f"Source: {source}", file=sys.stderr)
    print(f"Task Name: {task_name}", file=sys.stderr)
    print(f"Steps ({len(steps)}):", file=sys.stderr)
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    while True:
        # CRITICAL: Flush stderr before prompting to ensure any previous output is visible
        sys.stderr.flush()
        sys.stdout.flush()
        response = input("Proceed with these values? [y/N]: ").strip().lower()
        if response in ("y", "yes"):
            # Print newline after confirmation to ensure clean separation
            print("", file=sys.stderr)
            sys.stderr.flush()
            return True
        elif response in ("n", "no", ""):
            return False
        else:
            print("Please enter 'y' or 'n'", file=sys.stderr)


def extract_cluster_list(wrapper_path):
    """Extract Konflux user cluster contexts from the wrapper / kubeconfig.

    Returns:
        list: List of cluster context names
    """
    contexts, error = resolve_wrapper_contexts(wrapper_path)
    if error:
        print(f"Warning: Could not extract cluster list: {error}", file=sys.stderr)
        return []
    return select_analyzer_contexts(contexts)


def get_cluster_display_name(cluster_ctx):
    """Extract short cluster display name from full context string.

    Display-only. Operations still use the full context string.

    Examples:
        'default/api-stone-prd-rh01-pg1f-p1-openshiftapps-com:6443/smodak'
            -> 'stone-prd-rh01'
        'default/api-kflux-c-prd-e01-yo5u-p3-openshiftapps-com:443/smodak'
            -> 'kflux-c-prd-e01'

    Args:
        cluster_ctx: Full cluster context string

    Returns:
        str: Short cluster display name
    """
    match = _OPENSHIFTAPPS_CLUSTER_RE.search(cluster_ctx)
    if match:
        return match.group(1)
    match = re.search(r"/api-([^-]+-[^-]+-[^-]+)", cluster_ctx)
    if match:
        return match.group(1)
    if "/" in cluster_ctx:
        for part in cluster_ctx.split("/"):
            if "api-" in part:
                match = re.search(r"api-([^-]+-[^-]+-[^-]+)", part)
                if match:
                    return match.group(1)
    return cluster_ctx.split("/")[-1] if "/" in cluster_ctx else cluster_ctx


# Serializes spinner overwrites vs milestone (checkpoint) newlines on stderr.
