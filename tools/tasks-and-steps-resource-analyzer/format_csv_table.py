#!/usr/bin/env python3
"""
Format CSV output as a readable table while preserving CSV compatibility.
"""

import csv
import sys
from io import StringIO


def format_csv_as_table(csv_text):
    """Format CSV text as a readable table."""
    lines = [line for line in csv_text.strip().split("\n") if line.strip()]
    if not lines:
        return ""

    # Parse CSV
    reader = csv.reader(StringIO("\n".join(lines)))
    rows = list(reader)

    if not rows:
        return ""

    # Clean up values (remove quotes and spaces)
    cleaned_rows = []
    for row in rows:
        cleaned_row = [val.strip().strip('"') for val in row]
        cleaned_rows.append(cleaned_row)

    if not cleaned_rows:
        return ""

    # Calculate column widths
    num_cols = len(cleaned_rows[0])
    col_widths = [0] * num_cols

    for row in cleaned_rows:
        for i, val in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(str(val)))

    # Ensure minimum width for readability
    col_widths = [max(w, 8) for w in col_widths]

    # Format table
    output = []

    # Header row
    header = cleaned_rows[0]
    header_line = " | ".join(f"{val:<{col_widths[i]}}" for i, val in enumerate(header))
    output.append(header_line)
    output.append("-" * len(header_line))

    # Data rows
    for row in cleaned_rows[1:]:
        # Pad row to match header length
        padded_row = row + [""] * (num_cols - len(row))
        data_line = " | ".join(
            f"{str(val):<{col_widths[i]}}" for i, val in enumerate(padded_row[:num_cols])
        )
        output.append(data_line)

    return "\n".join(output)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Usage: format_csv_table.py [--raw]")
        print("  --raw: Output raw CSV instead of table format")
        sys.exit(0)

    raw_mode = "--raw" in sys.argv

    csv_data = sys.stdin.read()

    if raw_mode:
        # Just output the CSV as-is
        print(csv_data, end="")
    else:
        # Format as table
        table = format_csv_as_table(csv_data)
        print(table)
