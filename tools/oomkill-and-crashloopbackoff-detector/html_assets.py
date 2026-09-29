#!/usr/bin/env python3
"""
html_export.py

HTML export module for OOMKilled / CrashLoopBackOff detector.
Generates a standalone HTML report that can be opened directly in a browser.
"""

from __future__ import annotations


def _get_css_styles() -> str:
    """Return CSS styles for the HTML report."""
    return """
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont,
                'Segoe UI', Roboto, 'Helvetica Neue',
                Arial, sans-serif;
            background-color: #f5f5f5;
            color: #333;
            line-height: 1.6;
            padding: 20px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }

        .graph-section {
            margin-bottom: 32px;
        }

        .graph-section .chart-container {
            position: relative;
            min-height: 388px;
            padding: 0 20px;
        }

        .chart-scroll-wrap {
            max-width: 100%;
            overflow-x: auto;
            overflow-y: visible;
        }

        .chart-container-svg {
            display: block;
        }

        .inline-chart-svg {
            display: block;
        }

        .chart-x-label, .chart-y-label {
            fill: #6b7280;
        }

        .chart-x-label-vertical {
            white-space: nowrap;
        }

        .chart-value-label {
            font-weight: 600;
            fill: #374151;
        }

        .chart-legend {
            font-family: inherit;
        }

        .chart-empty {
            color: #6b7280;
            padding: 20px;
        }

        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
            font-family: Georgia, 'Times New Roman', Times, serif;
        }

        header .report-org-line {
            font-size: 2.25em;
            font-weight: bold;
            margin-bottom: 8px;
            letter-spacing: 0.04em;
        }

        header h1 {
            font-size: 1.75em;
            margin-bottom: 15px;
            letter-spacing: 0.02em;
        }

        .metadata {
            display: flex;
            justify-content: center;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 15px;
        }

        .badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 0.9em;
            font-weight: 600;
        }

        .badge-success {
            background-color: #10b981;
            color: white;
        }

        .badge-danger {
            background-color: #ef4444;
            color: white;
        }

        .badge-warning {
            background-color: #f59e0b;
            color: white;
        }

        .badge-info {
            background-color: #3b82f6;
            color: white;
        }

        .badge-oom {
            background-color: #dc2626;
            color: white;
        }

        .badge-crash {
            background-color: #ea580c;
            color: white;
        }

        main {
            padding: 30px;
        }

        section {
            margin-bottom: 40px;
        }

        h2 {
            color: #1f2937;
            margin-bottom: 20px;
            font-size: 1.5em;
            border-bottom: 2px solid #e5e7eb;
            padding-bottom: 10px;
        }

        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #6b7280;
            font-size: 1.2em;
        }

        .summary-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
            background: white;
        }

        .summary-table th,
        .summary-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e5e7eb;
        }

        .summary-table th {
            background-color: #dbeafe;
            font-weight: 600;
            color: #374151;
        }

        .summary-table tr:hover {
            background-color: #f9fafb;
        }

        .number {
            text-align: right;
            font-family: 'Courier New', monospace;
        }

        .details-table-wrap {
            max-width: 100%;
            overflow-x: auto;
            overflow-y: visible;
            margin-top: 15px;
        }

        .details-table {
            width: 100%;
            min-width: max-content;
            border-collapse: collapse;
            background: white;
            font-size: 0.9em;
        }

        .details-table th,
        .details-table td {
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid #e5e7eb;
        }

        .details-table th {
            background-color: #e0e7ff;
            font-weight: 600;
            color: #374151;
            position: sticky;
            top: 0;
            z-index: 10;
        }

        .sortable-header {
            cursor: pointer;
            user-select: none;
            position: relative;
            padding-right: 25px !important;
        }

        .sortable-header:hover {
            background-color: #c7d2fe !important;
        }

        .sort-indicator {
            position: absolute;
            right: 8px;
            font-size: 0.8em;
            color: #6b7280;
        }

        .sortable-header.sort-asc .sort-indicator::after {
            content: " ↑";
            color: #3b82f6;
        }

        .sortable-header.sort-desc .sort-indicator::after {
            content: " ↓";
            color: #3b82f6;
        }

        .sortable-header.sort-asc .sort-indicator,
        .sortable-header.sort-desc .sort-indicator {
            display: none;
        }

        .details-table tr:hover {
            background-color: #f9fafb;
        }

        .details-table .pod-name {
            font-family: 'Courier New', monospace;
            font-weight: 600;
            color: #1f2937;
        }

        .details-table .pod-type {
            text-align: center;
        }

        .details-table .pod-timestamps {
            font-size: 0.9em;
            color: #6b7280;
            white-space: pre-wrap;
        }

        .details-table .pod-sources {
            font-size: 0.85em;
            color: #6b7280;
            font-family: 'Courier New', monospace;
        }

        .details-table .pod-files {
            font-size: 0.85em;
        }

        .file-link {
            color: #3b82f6;
            text-decoration: none;
            padding: 4px 8px;
            border-radius: 3px;
            display: inline-block;
            transition: background-color 0.2s;
        }

        .file-link:hover {
            background-color: #dbeafe;
            text-decoration: underline;
        }

        footer {
            background-color: #f9fafb;
            padding: 20px;
            text-align: center;
            color: #6b7280;
            font-size: 0.9em;
            border-top: 1px solid #e5e7eb;
        }

        @media (max-width: 768px) {
            body {
                padding: 10px;
            }

            header h1 {
                font-size: 1.5em;
            }

            .metadata {
                flex-direction: column;
                align-items: center;
            }

            .details-table {
                font-size: 0.75em;
                display: block;
                overflow-x: auto;
            }

            .details-table th,
            .details-table td {
                padding: 6px 8px;
            }
        }
    """


def _get_sorting_javascript() -> str:
    """Return JavaScript code for table sorting functionality."""
    return """
        (function() {
            function makeSortable(table) {
                const headers = table.querySelectorAll('.sortable-header');
                let currentSort = { column: null, direction: 'asc' };

                headers.forEach((header, index) => {
                    header.addEventListener('click', function() {
                        const column = index;
                        const isAsc = currentSort.column === column
                            && currentSort.direction === 'asc';
                        const direction = isAsc ? 'desc' : 'asc';

                        // Remove sort classes from all headers
                        headers.forEach(h => {
                            h.classList.remove('sort-asc', 'sort-desc');
                        });

                        // Add sort class to current header
                        header.classList.add(direction === 'asc' ? 'sort-asc' : 'sort-desc');

                        // Sort the table
                        sortTable(table, column, direction);

                        // Update current sort
                        currentSort = { column, direction };
                    });
                });
            }

            function sortTable(table, column, direction) {
                const tbody = table.querySelector('tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));

                rows.sort((a, b) => {
                    const aText = a.cells[column].textContent.trim();
                    const bText = b.cells[column].textContent.trim();

                    // Try to parse as number first
                    const aNum = parseFloat(aText);
                    const bNum = parseFloat(bText);

                    let comparison = 0;
                    if (!isNaN(aNum) && !isNaN(bNum)) {
                        // Both are numbers
                        comparison = aNum - bNum;
                    } else {
                        // String comparison (case-insensitive)
                        comparison = aText.localeCompare(bText, undefined, {
                            numeric: true,
                            sensitivity: 'base'
                        });
                    }

                    return direction === 'asc' ? comparison : -comparison;
                });

                // Remove all rows from tbody
                rows.forEach(row => tbody.removeChild(row));

                // Add sorted rows back
                rows.forEach(row => tbody.appendChild(row));
            }

            // Initialize sorting when page loads
            document.addEventListener('DOMContentLoaded', function() {
                const table = document.querySelector('.sortable');
                if (table) {
                    makeSortable(table);
                }
            });

            // Also try immediately in case DOMContentLoaded already fired
            const table = document.querySelector('.sortable');
            if (table && table.querySelector('tbody')) {
                makeSortable(table);
            }
        })();
    """
