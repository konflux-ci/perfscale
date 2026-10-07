"""Filesystem roots for the analyzer tool directory."""

from pathlib import Path

# Parent of this package == tools/tasks-and-steps-resource-analyzer/
TOOL_DIR = Path(__file__).resolve().parent.parent
