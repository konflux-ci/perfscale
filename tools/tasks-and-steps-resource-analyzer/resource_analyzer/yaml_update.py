"""Optional YAML update / diff helpers (manual apply remains the default UX)."""

import argparse
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Semaphore, Thread

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

from .paths import TOOL_DIR

from .stats import cores_to_kubernetes, mb_to_kubernetes
from .task_yaml import normalize_step_name_for_compare

def generate_diff_patch(original_yaml, updated_yaml, file_path_or_url):
    """Generate a diff/patch file for remote YAML files."""
    script_dir = TOOL_DIR

    # Create temporary files
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as orig_file:
        yaml.dump(
            original_yaml,
            orig_file,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )
        orig_path = orig_file.name

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as upd_file:
        yaml.dump(updated_yaml, upd_file, default_flow_style=False, sort_keys=False, width=120)
        upd_path = upd_file.name

    try:
        # Generate diff using diff command
        result = subprocess.run(["diff", "-u", orig_path, upd_path], capture_output=True, text=True)

        # Extract filename from URL for patch file naming
        url_parts = file_path_or_url.split("/")
        filename = url_parts[-1] if url_parts else "buildah.yaml"
        # Remove .yaml extension and add timestamp
        base_name = filename.replace(".yaml", "").replace(".yml", "")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        patch_file = script_dir / f"{base_name}_{timestamp}.patch"

        # Extract relative path from URL for patch file context
        # e.g., "task/buildah/0.7/buildah.yaml" from the full URL
        relative_path = None
        if "/task/" in file_path_or_url:
            idx = file_path_or_url.find("/task/")
            relative_path = (
                file_path_or_url[idx + 1 :] if idx >= 0 else filename  # noqa: E203
            )

        # Write patch file with header
        with open(patch_file, "w") as f:
            f.write(f"# Resource limits patch for: {file_path_or_url}\n")
            f.write(f"# Generated: {datetime.now().isoformat()}\n")
            f.write(f"# File: {relative_path or filename}\n")
            f.write("#\n")
            f.write("# To apply this patch:\n")
            f.write("#   1. Download the original file from the URL above\n")
            f.write(f"#   2. Apply: patch <original_file> < {patch_file.name}\n")
            f.write("#   3. Or manually apply the changes shown below\n")
            f.write("#\n")
            # Replace temp file paths in diff with relative path
            diff_output = result.stdout
            if relative_path:
                # Replace the temp file paths with the relative path
                lines = diff_output.split("\n")
                if len(lines) >= 2:
                    f.write(f"--- a/{relative_path}\n")
                    f.write(f"+++ b/{relative_path}\n")
                    # Skip the first two lines (temp file paths) and write the rest
                    f.write("\n".join(lines[2:]))
                else:
                    f.write(diff_output)
            else:
                f.write(diff_output)

        print(f"\nGenerated patch file: {patch_file}", file=sys.stderr)
        print(f"Apply with: patch -p1 < {patch_file.name}", file=sys.stderr)

        return patch_file
    finally:
        # Clean up temp files
        os.unlink(orig_path)
        os.unlink(upd_path)


def update_yaml_file(yaml_path, recommendations, original_yaml, file_path_or_url=None, debug=False):
    """Update YAML file with recommended resource limits, preserving original formatting."""
    updated = False

    # Build a map of step names to their resource recommendations
    step_updates = {}
    for rec in recommendations:
        if rec is None:
            continue

        step_name_csv = rec["step_name"]  # e.g., "step-build"
        mem_k8s = rec["mem_recommended_k8s"]
        cpu_k8s = rec["cpu_recommended_k8s"]

        # Convert "step-build" to "build" for YAML matching
        step_name_yaml = normalize_step_name_for_compare(step_name_csv)
        step_updates[step_name_yaml] = {"memory": mem_k8s, "cpu": cpu_k8s}

    if not step_updates:
        return False

    if debug:
        print(f"DEBUG: Looking for steps: {list(step_updates.keys())}", file=sys.stderr)

    # If remote URL, use the old method (generate patch)
    if file_path_or_url and (
        file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://")
    ):
        updated_yaml = yaml.safe_load(yaml.dump(original_yaml))  # Deep copy

        for step_name_yaml, resources in step_updates.items():
            steps = updated_yaml.get("spec", {}).get("steps", [])
            for step in steps:
                if step.get("name") == step_name_yaml:
                    if "computeResources" not in step:
                        step["computeResources"] = {}
                    if "limits" not in step["computeResources"]:
                        step["computeResources"]["limits"] = {}
                    if "requests" not in step["computeResources"]:
                        step["computeResources"]["requests"] = {}

                    step["computeResources"]["limits"]["memory"] = resources["memory"]
                    step["computeResources"]["limits"]["cpu"] = resources["cpu"]
                    step["computeResources"]["requests"]["memory"] = resources["memory"]
                    step["computeResources"]["requests"]["cpu"] = resources["cpu"]
                    updated = True
                    print(
                        f"Updated step '{step_name_yaml}': memory={resources['memory']},"
                        f" cpu={resources['cpu']}",
                        file=sys.stderr,
                    )
                    break

        if updated:
            generate_diff_patch(original_yaml, updated_yaml, file_path_or_url)
        return updated

    # Note: debug parameter is not used for remote URLs (patch generation)

    # For local files, update by modifying text directly to preserve formatting
    if yaml_path and not yaml_path.startswith("http"):
        # Read the file as text
        with open(yaml_path) as f:
            lines = f.readlines()

        # First pass: collect all step positions (process in reverse to avoid index shifting)
        step_positions = []
        i = 0
        in_steps_section = False
        while i < len(lines):
            line = lines[i]

            # Track if we're in the steps section
            if re.match(r"^\s+steps:\s*$", line):
                in_steps_section = True
                if debug:
                    print(f"DEBUG: Entered steps section at line {i + 1}", file=sys.stderr)
            elif in_steps_section:
                line_indent = len(line) - len(line.lstrip())
                if (
                    line_indent <= 2
                    and line.strip()
                    and not line.strip().startswith("-")
                    and not line.strip().startswith("#")
                ) and ("workspaces:" in line or "results:" in line or "volumes:" in line):
                    in_steps_section = False
                    if debug:
                        print(
                            f"DEBUG: Left steps section at line {i + 1}: {line.strip()}",
                            file=sys.stderr,
                        )

            # Look for step names
            if in_steps_section:
                line_indent = len(line) - len(line.lstrip())
                is_step = False
                step_name_match = None

                # Pattern 1: "    - name: stepname" or "  - name: stepname" (2 or 4 spaces: 2 for
                # steps:, 0-2 for list item)
                if re.match(r"^\s+-\s+name:\s+", line) and line_indent in (2, 4):
                    is_step = True
                    step_name_match = re.search(r"name:\s+(.+)$", line)
                # Pattern 2: "    name: stepname" (not starting with "-")
                elif (
                    re.match(r"^\s+name:\s+", line)
                    and line_indent in (2, 4)
                    and not line.strip().startswith("-")
                ):
                    for lookback in range(1, min(11, i + 1)):
                        prev_line = lines[i - lookback]
                        prev_stripped = prev_line.lstrip()
                        prev_indent = len(prev_line) - len(prev_stripped)
                        if not prev_stripped:
                            continue
                        if prev_indent == 4 and prev_stripped.endswith(":"):
                            is_step = False
                            break
                        if (
                            prev_indent == 2
                            and prev_stripped.startswith("-")
                            and (
                                "image:" in prev_line
                                or prev_stripped.startswith("- image:")
                                or prev_stripped.startswith("- name:")
                            )
                        ):
                            is_step = True
                            break
                        if prev_indent <= 2 and (
                            prev_stripped.startswith("steps:")
                            or (
                                prev_stripped
                                and not prev_stripped.startswith("-")
                                and ":" in prev_stripped
                            )
                        ):
                            break
                    if is_step:
                        step_name_match = re.search(r"name:\s+(.+)$", line)

                if step_name_match and is_step:
                    step_name = step_name_match.group(1).strip()
                    if step_name in step_updates:
                        step_positions.append((i, step_name))

            i += 1

        # Second pass: process steps in reverse order (bottom to top) to avoid index shifting
        step_positions.sort(reverse=True)  # Process from bottom to top

        # Process each step
        for step_line_idx, step_name in step_positions:
            # Find the current line number for this step (may have shifted due to previous
            # insertions)
            # Search more broadly - start from a reasonable position and search forward/backward
            current_step_line = None

            # Calculate approximate shift: each step processed before this one (in reverse order)
            # may have added ~7 lines
            steps_before = sum(1 for idx, name in step_positions if idx > step_line_idx)
            estimated_shift = steps_before * 7  # Approximate lines added per step
            search_center = step_line_idx + estimated_shift

            # Search in a wider range
            search_start = max(0, search_center - 50)
            search_end = min(len(lines), search_center + 100)

            # First try near the estimated position
            for search_idx in range(search_start, search_end):
                if search_idx < len(lines):
                    search_line = lines[search_idx]
                    line_indent = len(search_line) - len(search_line.lstrip())
                    # Check Pattern 1: "    - name: stepname" or "  - name: stepname" (2 or 4
                    # spaces: 2 for steps:, 0-2 for list item)
                    if re.match(r"^\s+-\s+name:\s+", search_line) and line_indent in (
                        2,
                        4,
                    ):
                        name_match = re.search(r"name:\s+(.+)$", search_line)
                        if name_match and name_match.group(1).strip() == step_name:
                            current_step_line = search_idx
                            break
                    # Check Pattern 2: "    name: stepname" (not starting with "-")
                    elif (
                        re.match(r"^\s+name:\s+", search_line)
                        and line_indent in (2, 4)
                        and not search_line.strip().startswith("-")
                    ):
                        # Verify it's a step name by checking previous lines
                        is_step_name = False
                        for lookback in range(1, min(6, search_idx + 1)):
                            prev_line = lines[search_idx - lookback]
                            prev_stripped = prev_line.lstrip()
                            prev_indent = len(prev_line) - len(prev_stripped)
                            if (
                                prev_indent == 2
                                and prev_stripped.startswith("-")
                                and "image:" in prev_line
                            ):
                                is_step_name = True
                                break
                            if prev_indent <= 2:
                                break
                        if is_step_name:
                            name_match = re.search(r"name:\s+(.+)$", search_line)
                            if name_match and name_match.group(1).strip() == step_name:
                                current_step_line = search_idx
                                break

            # If not found, do a broader search through the entire steps section
            if current_step_line is None:
                # Find steps: line first
                steps_section_start = None
                for idx, line in enumerate(lines):
                    if re.match(r"^\s+steps:\s*$", line):
                        steps_section_start = idx
                        break

                if steps_section_start is not None:
                    # Search from steps: to end of file (or next top-level section)
                    for search_idx in range(steps_section_start + 1, len(lines)):
                        search_line = lines[search_idx]
                        line_indent = len(search_line) - len(search_line.lstrip())
                        # Stop if we've left steps section
                        if (
                            line_indent <= 2
                            and search_line.strip()
                            and not search_line.strip().startswith("-")
                            and not search_line.strip().startswith("#")
                        ) and (
                            "workspaces:" in search_line
                            or "results:" in search_line
                            or "volumes:" in search_line
                        ):
                            break

                        # Check for step name (2 or 4 spaces: 2 for steps:, 0-2 for list item)
                        if re.match(r"^\s+-\s+name:\s+", search_line) and line_indent in (2, 4):
                            name_match = re.search(r"name:\s+(.+)$", search_line)
                            if name_match and name_match.group(1).strip() == step_name:
                                current_step_line = search_idx
                                break
                        elif (
                            re.match(r"^\s+name:\s+", search_line)
                            and line_indent in (2, 4)
                            and not search_line.strip().startswith("-")
                        ):
                            name_match = re.search(r"name:\s+(.+)$", search_line)
                            if name_match and name_match.group(1).strip() == step_name:
                                # Verify it's a step
                                for lookback in range(1, min(6, search_idx + 1)):
                                    prev_line = lines[search_idx - lookback]
                                    if (
                                        len(prev_line) - len(prev_line.lstrip()) == 2
                                        and prev_line.lstrip().startswith("-")
                                        and "image:" in prev_line
                                    ):
                                        current_step_line = search_idx
                                        break
                                if current_step_line is not None:
                                    break

            if current_step_line is None:
                if debug:
                    print(
                        f"WARNING: Could not find step '{step_name}' (original line"
                        f" {step_line_idx + 1}), skipping",
                        file=sys.stderr,
                    )
                continue

            line = lines[current_step_line]
            if debug:
                print(
                    f"DEBUG: Processing step '{step_name}' at line {current_step_line + 1}",
                    file=sys.stderr,
                )

            # Check if this step needs updating
            if step_name in step_updates:
                resources = step_updates[step_name]
                if debug:
                    print(
                        f"DEBUG: Processing step '{step_name}', looking for computeResources...",
                        file=sys.stderr,
                    )

                # Look ahead for computeResources section within this step
                j = current_step_line + 1
                step_line = lines[current_step_line]
                step_indent = len(step_line) - len(step_line.lstrip())

                # Find the actual indent for step content fields (like image:, computeResources:)
                # Step content should be indented 4 spaces (same as image:, env:, etc.)
                step_content_indent = step_indent + 2  # Default: 2 spaces more than step name
                # Look for image: or other step fields to determine correct indent
                for k in range(current_step_line, min(current_step_line + 10, len(lines))):
                    check_line = lines[k]
                    check_indent = len(check_line) - len(check_line.lstrip())
                    if "image:" in check_line or "env:" in check_line or "script:" in check_line:
                        step_content_indent = check_indent
                        break

                in_compute_resources = False
                in_limits = False
                in_requests = False
                limits_indent = None
                requests_indent = None
                memory_updated_limits = False
                cpu_updated_limits = False
                memory_updated_requests = False
                cpu_updated_requests = False
                compute_resources_found = False
                compute_resources_insert_pos = None
                compute_resources_indent = None  # Will be set when we find computeResources

                while j < len(lines):
                    next_line = lines[j]
                    next_stripped = next_line.lstrip()
                    next_indent = len(next_line) - len(next_stripped)

                    # Stop if we've moved to the next step (same or less indent with "- name:" or
                    # "name:")
                    if next_indent <= step_indent and (
                        next_stripped.startswith("- name:") or next_stripped.startswith("name:")
                    ):
                        break

                    # Stop if we've moved to a different top-level section
                    if next_indent < step_indent:
                        break

                    # Find computeResources section (at same indent level as step content fields
                    # like image:)
                    # Allow some flexibility in indent to handle formatting variations
                    if "computeResources:" in next_line and (
                        next_indent == step_content_indent
                        or (step_content_indent <= 4 and next_indent >= 2 and next_indent <= 6)
                    ):
                        in_compute_resources = True
                        compute_resources_found = True
                        compute_resources_indent = (
                            next_indent  # Store the actual indent of computeResources
                        )
                        if debug:
                            print(
                                f"DEBUG: Found computeResources at line"
                                f" {j + 1}, indent={next_indent}"
                                f" (step_content_indent="
                                f"{step_content_indent})",
                                file=sys.stderr,
                            )
                        j += 1
                        continue

                    # Track where to insert computeResources if not found (after name: and image:,
                    # before other fields)
                    if not compute_resources_found and compute_resources_insert_pos is None:
                        # Stop if we've left the step (hit next step marker or back to step list
                        # level)
                        if next_indent <= step_indent:
                            # We've left the step, insert before leaving (at the end of current
                            # step)
                            # Find the last field of current step by going back
                            for k in range(j - 1, max(current_step_line, j - 20), -1):
                                if k < len(lines):
                                    check_line = lines[k]
                                    check_indent = len(check_line) - len(check_line.lstrip())
                                    if (
                                        check_indent == step_content_indent
                                        and check_line.strip()
                                        and not check_line.strip().startswith("#")
                                    ):
                                        compute_resources_insert_pos = k + 1
                                        break
                            if compute_resources_insert_pos is None:
                                # Fallback: insert right before leaving the step
                                compute_resources_insert_pos = j
                            break
                        # Insert after name: or image: (whichever comes last)
                        elif "image:" in next_line or "name:" in next_line:
                            compute_resources_insert_pos = j + 1
                        elif (
                            next_stripped
                            and not next_stripped.startswith("#")
                            and next_indent == step_content_indent
                        ):
                            # We've hit another field at step content level, insert before it
                            compute_resources_insert_pos = j
                            break

                    if not in_compute_resources:
                        j += 1
                        continue

                    # Stop if we've left computeResources section (back to step level or next step)
                    # computeResources is at step_indent, so if we go back to step_indent or less,
                    # we've left it
                    if (
                        next_indent <= step_indent
                        and next_stripped
                        and not next_stripped.startswith("#")
                        and "computeResources" not in next_line
                    ):
                        break

                    # Find limits section (should be indented 2 spaces more than computeResources)
                    # Use compute_resources_indent if available, otherwise fall back to step_indent
                    # + 2
                    expected_limits_indent = (
                        compute_resources_indent + 2
                        if compute_resources_indent is not None
                        else step_indent + 2
                    )
                    if "limits:" in next_line and next_indent == expected_limits_indent:
                        in_limits = True
                        in_requests = False
                        limits_indent = next_indent
                        if debug:
                            print(
                                f"DEBUG: Found limits at line {j + 1}, indent={next_indent}",
                                file=sys.stderr,
                            )
                        j += 1
                        continue

                    # Find requests section (should be indented 2 spaces more than computeResources)
                    # Use compute_resources_indent if available, otherwise fall back to step_indent
                    # + 2
                    expected_requests_indent = (
                        compute_resources_indent + 2
                        if compute_resources_indent is not None
                        else step_indent + 2
                    )
                    if "requests:" in next_line and next_indent == expected_requests_indent:
                        # Before leaving limits section, add missing fields
                        if in_limits and not cpu_updated_limits:
                            indent = " " * (limits_indent + 2)
                            lines.insert(j, f"{indent}cpu: {resources['cpu']}\n")
                            cpu_updated_limits = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Added cpu to limits at line {j + 1}:"
                                    f" {resources['cpu']}",
                                    file=sys.stderr,
                                )
                            j += 1

                        in_requests = True
                        in_limits = False
                        requests_indent = next_indent
                        if debug:
                            print(
                                f"DEBUG: Found requests at line {j + 1}, indent={next_indent}",
                                file=sys.stderr,
                            )
                        j += 1
                        continue

                    # Update memory and cpu values
                    if in_limits:
                        # Update memory in limits (should be indented 2 spaces more than limits:)
                        if (
                            re.match(r"^\s+memory:\s+", next_line)
                            and next_indent == limits_indent + 2
                        ):
                            indent = next_line[: len(next_line) - len(next_line.lstrip())]
                            lines[j] = f"{indent}memory: {resources['memory']}\n"
                            memory_updated_limits = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Updated memory in limits at line {j + 1}:"
                                    f" {resources['memory']}",
                                    file=sys.stderr,
                                )

                        # Update cpu in limits (should be indented 2 spaces more than limits:)
                        elif (
                            re.match(r"^\s+cpu:\s+", next_line) and next_indent == limits_indent + 2
                        ):
                            indent = next_line[: len(next_line) - len(next_line.lstrip())]
                            lines[j] = f"{indent}cpu: {resources['cpu']}\n"
                            cpu_updated_limits = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Updated cpu in limits at line {j + 1}:"
                                    f" {resources['cpu']}",
                                    file=sys.stderr,
                                )

                        # Check if we need to add missing fields (after limits: but before
                        # requests:)
                        elif limits_indent is not None and next_indent > limits_indent:
                            # Still in limits section, check if we need to add fields
                            if not memory_updated_limits and (
                                "requests:" in next_line or next_indent <= limits_indent
                            ):
                                # Insert memory before leaving limits section
                                indent = " " * (limits_indent + 2)
                                lines.insert(j, f"{indent}memory: {resources['memory']}\n")
                                memory_updated_limits = True
                                updated = True
                                j += 1
                            elif not cpu_updated_limits and (
                                "requests:" in next_line or next_indent <= limits_indent
                            ):
                                # Insert cpu before leaving limits section
                                indent = " " * (limits_indent + 2)
                                lines.insert(j, f"{indent}cpu: {resources['cpu']}\n")
                                cpu_updated_limits = True
                                updated = True
                                j += 1

                    elif in_requests:
                        # Update memory in requests (should be indented 2 spaces more than
                        # requests:)
                        if (
                            re.match(r"^\s+memory:\s+", next_line)
                            and next_indent == requests_indent + 2
                        ):
                            indent = next_line[: len(next_line) - len(next_line.lstrip())]
                            lines[j] = f"{indent}memory: {resources['memory']}\n"
                            memory_updated_requests = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Updated memory in requests at line {j + 1}:"
                                    f" {resources['memory']}",
                                    file=sys.stderr,
                                )

                        # Update cpu in requests (should be indented 2 spaces more than requests:)
                        elif (
                            re.match(r"^\s+cpu:\s+", next_line)
                            and next_indent == requests_indent + 2
                        ):
                            indent = next_line[: len(next_line) - len(next_line.lstrip())]
                            lines[j] = f"{indent}cpu: {resources['cpu']}\n"
                            cpu_updated_requests = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Updated cpu in requests at line {j + 1}:"
                                    f" {resources['cpu']}",
                                    file=sys.stderr,
                                )

                    # Stop if we've left computeResources section (back to step level)
                    if (
                        in_compute_resources
                        and next_indent <= step_indent
                        and next_stripped
                        and not next_stripped.startswith("#")
                        and "computeResources" not in next_line
                    ):
                        # Add missing fields before leaving
                        if in_limits and not cpu_updated_limits:
                            indent = (
                                " " * (limits_indent + 2)
                                if limits_indent
                                else " " * (step_content_indent + 2)
                            )
                            lines.insert(j, f"{indent}cpu: {resources['cpu']}\n")
                            cpu_updated_limits = True
                            updated = True
                            if debug:
                                print(
                                    f"DEBUG: Added cpu to limits before leaving"
                                    f" computeResources: {resources['cpu']}",
                                    file=sys.stderr,
                                )
                            j += 1
                        if in_requests:
                            if not memory_updated_requests:
                                indent = (
                                    " " * (requests_indent + 2)
                                    if requests_indent
                                    else " " * (step_content_indent + 2)
                                )
                                lines.insert(j, f"{indent}memory: {resources['memory']}\n")
                                memory_updated_requests = True
                                updated = True
                                if debug:
                                    print(
                                        f"DEBUG: Added memory to requests before leaving"
                                        f" computeResources: {resources['memory']}",
                                        file=sys.stderr,
                                    )
                                j += 1
                            if not cpu_updated_requests:
                                indent = (
                                    " " * (requests_indent + 2)
                                    if requests_indent
                                    else " " * (step_content_indent + 2)
                                )
                                lines.insert(j, f"{indent}cpu: {resources['cpu']}\n")
                                cpu_updated_requests = True
                                updated = True
                                if debug:
                                    print(
                                        f"DEBUG: Added cpu to requests before leaving"
                                        f" computeResources: {resources['cpu']}",
                                        file=sys.stderr,
                                    )
                                j += 1
                        break

                    j += 1

                # If computeResources wasn't found, create it
                if not compute_resources_found and compute_resources_insert_pos is not None:
                    indent = " " * step_content_indent
                    compute_resources_block = [
                        f"{indent}computeResources:\n",
                        f"{indent}  limits:\n",
                        f"{indent}    memory: {resources['memory']}\n",
                        f"{indent}    cpu: {resources['cpu']}\n",
                        f"{indent}  requests:\n",
                        f"{indent}    memory: {resources['memory']}\n",
                        f"{indent}    cpu: {resources['cpu']}\n",
                    ]
                    # Insert the block
                    for idx, new_line in enumerate(compute_resources_block):
                        lines.insert(compute_resources_insert_pos + idx, new_line)
                    updated = True
                    if debug:
                        print(
                            f"DEBUG: Created computeResources for step '{step_name}'",
                            file=sys.stderr,
                        )

                if updated:
                    print(
                        f"Updated step '{step_name}': memory={resources['memory']},"
                        f" cpu={resources['cpu']}",
                        file=sys.stderr,
                    )

        # Write back the modified file
        if updated:
            with open(yaml_path, "w") as f:
                f.writelines(lines)
            print(f"\nUpdated YAML file: {yaml_path}", file=sys.stderr)

        return updated

    return False


