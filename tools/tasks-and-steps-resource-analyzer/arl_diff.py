from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from arl_constants import (
    yaml,
)


def generate_diff_patch(original_yaml, updated_yaml, file_path_or_url):
    """Generate a diff/patch file for remote YAML files."""
    script_dir = Path(__file__).parent

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
